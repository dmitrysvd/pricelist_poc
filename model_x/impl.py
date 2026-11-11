"""Модель X: реализация операций интерфейса сырым SQL.

Напоминание о схеме: x_row — полное состояние всех цен услуги начиная с valid_from.
  price — основная цена, price_overrides — JSON {"<id типа>": цена} для прочих типов;
  child_id — следующая строка той же услуги (связный список); хвост = child_id IS NULL.
  Запланированная строка (valid_from в будущем) до применения к цепочке не привязана.
Значение одного типа в строке: основной — колонка price, прочие — ключ JSON,
поэтому SQL для одного типа собирается из двух вариантов выражений (main / override).
Индексы, на которые опираются запросы:
  x_row_tail_idx       (service_id, valid_from) INCLUDE (price) WHERE child_id IS NULL
  x_row_service_vf_idx (service_id, valid_from)
  x_row_child_id_key   UNIQUE (child_id)
  x_row_valid_from_brin BRIN (valid_from)
"""
from bench.common_ops import ADD_TYPE, IMPORT, TYPE_MAP
from bench.iface import Edit, Op, PriceModel, Ref, Step

OPEN_DAILY = """
-- Открыть дневную версию (филиал, день, сотрудник) или взять уже открытую.
-- Вливать правки можно только в версию, которая сейчас последняя применённая
-- в филиале: иначе строки одной услуги шли бы в цепочке не в порядке версий.
WITH cur AS (
    SELECT id FROM x_version
    WHERE branch_id = %(b)s AND status = 'A' AND author_id = %(a)s
      AND created_at >= date_trunc('day', now())
), demote AS (
    UPDATE x_version SET status = 'O'
    WHERE branch_id = %(b)s AND status = 'A' AND NOT EXISTS (SELECT 1 FROM cur)
    RETURNING 1
), ins AS (
    -- row_ids и price_type_ids наполняются по мере правок
    -- (SELECT count(*) FROM demote): основной оператор ссылается на demote, поэтому
    -- Postgres выполняет его до конца раньше вставки. Без ссылки CTE выполнился бы
    -- в конце оператора, и новая 'A' нарушила бы UNIQUE (branch_id) WHERE status='A'.
    INSERT INTO x_version (branch_id, author_id, created_at, day, status, last_change_at, row_ids, price_type_ids)
    SELECT %(b)s, %(a)s, now(), current_date, 'A', now(), '{}', '{}'
    WHERE NOT EXISTS (SELECT 1 FROM cur) AND (SELECT count(*) FROM demote) >= 0
    RETURNING id
)
SELECT id FROM cur UNION ALL SELECT id FROM ins
"""

# Актуальная строка: хвост цепочки, уже вступивший в силу. Хвост с будущим
# valid_from — запланированная, ещё не применённая строка.
CURRENT = "r.child_id IS NULL AND r.valid_from <= now()"


def val(alias, is_main):
    """Значение типа %(key)s в строке alias (numeric или NULL)."""
    return f"{alias}.price" if is_main else f"({alias}.price_overrides ->> %(key)s)::numeric"


def filled(alias, is_main):
    return "true" if is_main else f"{alias}.price_overrides ? %(key)s"


def set_cols(alias, is_main, new):
    """(price, price_overrides) строки alias с подменённым значением типа на new."""
    if is_main:
        return f"{new}", f"{alias}.price_overrides"
    return (f"{alias}.price",
            f"CASE WHEN {new} IS NULL THEN {alias}.price_overrides - %(key)s "
            f"ELSE {alias}.price_overrides || jsonb_build_object(%(key)s, {new}) END")


def write_one_type(svc_filter, new_expr, is_main, only_filled):
    """SQL: записать новое значение одного типа для набора услуг в дневную версию."""
    p_new, o_new = set_cols("t", is_main, "t.new_value")
    p_upd, o_upd = set_cols("r", is_main, "t.new_value")
    p_sch, o_sch = set_cols("q", is_main, "t.new_value")
    return f"""
-- Записать новое значение одного типа (ключ key) для набора услуг в дневную версию vid.
WITH cur AS (
  -- актуальные строки услуг (частичный индекс хвостов x_row_tail_idx)
  SELECT r.id, r.service_id, r.price, r.price_overrides FROM x_row r JOIN service s ON s.id = r.service_id
  WHERE {svc_filter} AND {CURRENT} {"AND " + filled("r", is_main) if only_filled else ""}
), sch AS (
  -- запланированные (ещё не привязанные) строки тех же услуг: хвост с будущим valid_from
  SELECT q.id, q.service_id, q.price, q.price_overrides FROM x_row q JOIN cur ON cur.service_id = q.service_id
  WHERE q.child_id IS NULL AND q.valid_from > now()
), mine AS (
  -- строки, уже созданные этой дневной версией (массив разворачивается один раз)
  SELECT unnest(row_ids) AS id FROM x_version WHERE id = %(vid)s
), t AS (
  SELECT cur.*, {new_expr} AS new_value,
         -- строка услуги уже создана сегодня этой версией -> правим её на месте
         cur.id IN (SELECT id FROM mine) AS inplace,
         nextval('x_row_id_seq') AS new_id
  FROM cur
  -- этот тип у услуги запланирован (в запланированном снимке он отличается
  -- от текущего) -> цену менять нельзя, услугу пропускаем
  WHERE NOT EXISTS (SELECT 1 FROM sch WHERE sch.service_id = cur.service_id
                    AND {val("sch", is_main)} IS DISTINCT FROM {val("cur", is_main)})
), upd AS (
  -- слияние правок одного дня: обновить строку, созданную этой же версией
  UPDATE x_row r SET price = {p_upd}, price_overrides = {o_upd}
  FROM t WHERE r.id = t.id AND t.inplace
  RETURNING 1
), ins AS (
  -- иначе новая строка: копия текущего снимка с одним изменённым значением
  INSERT INTO x_row (id, service_id, valid_from, price, price_overrides)
  SELECT t.new_id, t.service_id, now(), {p_new}, {o_new} FROM t WHERE NOT t.inplace
  RETURNING id
), link AS (
  -- прежняя актуальная строка указывает на новую (child) и перестаёт быть хвостом
  UPDATE x_row r SET child_id = t.new_id FROM t WHERE r.id = t.id AND NOT t.inplace
), rebase AS (
  -- запланированная строка — снимок всех типов на момент планирования;
  -- вписываем в неё новое значение, иначе применение откатило бы эту правку
  UPDATE x_row q SET price = {p_sch}, price_overrides = {o_sch}
  FROM sch JOIN t ON t.service_id = sch.service_id WHERE q.id = sch.id
), ver AS (
  -- версия запоминает свои строки и изменённые типы (для фильтра истории)
  UPDATE x_version SET
    row_ids = row_ids || ARRAY(SELECT new_id FROM t WHERE NOT inplace),
    price_type_ids = ARRAY(SELECT DISTINCT x FROM unnest(price_type_ids || %(tid)s::int) x),
    last_change_at = now()
  WHERE id = %(vid)s
)
SELECT (SELECT count(*) FROM ins) AS inserted, (SELECT count(*) FROM upd) AS updated_in_place
"""


NEW_VERSION = """
-- Отдельная новая версия (восстановление, применение к филиалу, импорт).
WITH demote AS (
    UPDATE x_version SET status = 'O' WHERE branch_id = %(b)s AND status = 'A' RETURNING 1
)
-- (SELECT count(*) FROM demote): основной оператор ссылается на demote, поэтому
-- Postgres выполняет его до конца раньше вставки. Без ссылки CTE выполнился бы
-- в конце оператора, и новая 'A' нарушила бы UNIQUE (branch_id) WHERE status='A'.
INSERT INTO x_version (branch_id, author_id, created_at, day, status, last_change_at, restored_from_id,
                       row_ids, price_type_ids)
SELECT %(b)s, %(a)s, now(), current_date, 'A', now(), %(restored)s, '{}', '{}'
WHERE (SELECT count(*) FROM demote) >= 0
RETURNING id
"""

CANCEL_ALL = [
    ("cancel_rows", """
-- Отмена всех запланированных версий филиала: их строки не привязаны к цепочкам
-- (на них никто не ссылается через child), поэтому просто удаляем по row_ids.
DELETE FROM x_row r USING (
  SELECT unnest(row_ids) AS id FROM x_version WHERE branch_id = %(b)s AND status = 'S'
) d WHERE r.id = d.id
"""),
    ("cancel_versions", "DELETE FROM x_version WHERE branch_id = %(b)s AND status = 'S'"),
]


def write_snapshots(t_cte, tids="%(tids)s::int[]"):
    """SQL: t (id текущей строки, service_id, new_price, new_overrides) -> новые снимки в версии."""
    return t_cte + """
-- Записать готовые снимки t: новая строка на услугу + привязка к цепочке.
, n AS (SELECT t.*, nextval('x_row_id_seq') AS new_id FROM t),
ins AS (
  INSERT INTO x_row (id, service_id, valid_from, price, price_overrides)
  SELECT new_id, service_id, now(), new_price, new_overrides FROM n
  RETURNING id
), link AS (
  -- прежняя актуальная строка -> child = новая
  UPDATE x_row r SET child_id = n.new_id FROM n WHERE r.id = n.id
), ver AS (
  UPDATE x_version SET row_ids = row_ids || ARRAY(SELECT new_id FROM n),
         price_type_ids = """ + tids + """, last_change_at = now()
  WHERE id = %(vid)s
)
SELECT (SELECT count(*) FROM ins) AS inserted
"""


def page_where(cursor, extra=""):
    # Курсорная пагинация по индексу service_page_idx: сравнение кортежей
    # (category_sort, code, name, id) > курсора продолжает обход индекса с места,
    # где закончилась прошлая страница (без OFFSET).
    cond = "s.branch_id = %(b)s AND NOT s.archived" + extra
    if cursor:
        cond += " AND (s.category_sort, s.code, s.name, s.id) > (%(c1)s, %(c2)s, %(c3)s, %(c4)s)"
    return cond


def cursor_params(cursor):
    return dict(zip(("c1", "c2", "c3", "c4"), cursor)) if cursor else {}


# Срез на момент t (формула из описания X): строка услуги действует в t, если
# она началась не позже t и следующая (child) либо отсутствует, либо началась позже t.
# Перебирает строки услуги с valid_from <= t по x_row_service_vf_idx и для каждой
# смотрит child по первичному ключу.
SLICE = """
  SELECT r.* FROM x_row r LEFT JOIN x_row c ON c.id = r.child_id
  WHERE r.service_id = {svc} AND r.valid_from <= {t} AND (c.id IS NULL OR c.valid_from > {t})
"""


class XModel(PriceModel):
    name = "X"

    # ------------------------------------------------------------- чтение
    def current_prices(self, service_ids, main_type, ins_type):
        sql = f"""
-- Горячий путь: одна строка на услугу из частичного индекса хвостов,
-- в ней сразу все типы — основная из колонки, страховая из JSON по ключу.
SELECT s.id, s.code, s.name, r.price AS main_price, (r.price_overrides ->> %(ins)s)::numeric AS ins_price
FROM service s
JOIN x_row r ON r.service_id = s.id AND {CURRENT}
WHERE s.id = ANY(%(ids)s)
"""
        return Op([Step("q", sql, {"ids": service_ids, "ins": str(ins_type) if ins_type else None})])

    def price_page(self, branch, cursor, limit, type_ids, missing_type=None):
        extra = ""
        if missing_type:
            # «Только без цены»: в актуальном снимке нет ключа этого типа.
            extra = " AND NOT (r.price_overrides ? %(mt)s)"
        sql = f"""
-- Страница прайса: 50 услуг по индексу service_page_idx, к каждой — актуальная
-- строка (все типы сразу) и ближайшая запланированная.
SELECT s.id, s.category_sort, s.code, s.name, r.price, r.price_overrides,
       sch.valid_from AS sch_at, sch.price AS sch_price, sch.price_overrides AS sch_overrides
FROM service s
JOIN x_row r ON r.service_id = s.id AND {CURRENT}
-- запланированная строка — полный снимок; какие именно цены в ней изменены,
-- приложение видит сравнением с актуальной (50 строк, на клиенте)
LEFT JOIN LATERAL (
  SELECT q.valid_from, q.price, q.price_overrides FROM x_row q
  WHERE q.service_id = s.id AND q.child_id IS NULL AND q.valid_from > now()
  ORDER BY q.valid_from LIMIT 1
) sch ON true
WHERE {page_where(cursor, extra)}
ORDER BY s.category_sort, s.code, s.name, s.id
LIMIT %(lim)s
"""
        return Op([Step("q", sql, {"b": branch, "lim": limit, "mt": str(missing_type) if missing_type else None,
                                   **cursor_params(cursor)})])

    def version_page(self, branch, version_id, cursor, limit, type_ids):
        sql = f"""
-- «Прайс-лист от версии»: срез на момент последней правки версии.
WITH v AS (SELECT last_change_at AS t FROM x_version WHERE id = %(vid)s),
page AS (
  SELECT s.id, s.category_sort, s.code, s.name FROM service s
  WHERE {page_where(cursor)}
  ORDER BY s.category_sort, s.code, s.name, s.id LIMIT %(lim)s
),
sl AS (
  -- строка, действовавшая в момент t, по формуле среза X (одна на услугу)
  SELECT p.id AS service_id, p.category_sort, p.code, p.name, r.id, r.price, r.price_overrides
  FROM page p CROSS JOIN v
  JOIN LATERAL ({SLICE.format(svc="p.id", t="v.t")}) r ON true
)
SELECT sl.service_id, sl.code, sl.name, sl.price, sl.price_overrides, ch.id IS NOT NULL AS changed,
       par.price AS old_price, par.price_overrides AS old_overrides
FROM sl
-- «изменено в этой версии» = id строки есть в row_ids версии. Массив
-- разворачивается в хеш один раз; сравнение r.id = ANY(row_ids) на каждую
-- строку сканировало бы весь массив (у массовой версии 85 тыс. id) 50 раз.
LEFT JOIN (SELECT unnest(row_ids) AS id FROM x_version WHERE id = %(vid)s) ch ON ch.id = sl.id
-- старые цены — строка-родитель (её child = текущая), по уникальному индексу child_id;
-- какие именно типы изменены, приложение видит сравнением двух снимков
LEFT JOIN x_row par ON par.child_id = sl.id AND ch.id IS NOT NULL
ORDER BY sl.category_sort, sl.code, sl.name, sl.service_id
"""
        return Op([Step("q", sql, {"b": branch, "vid": version_id, "lim": limit, **cursor_params(cursor)})])

    def history(self, branch, date_from, date_to, author, price_type, limit):
        cond = ["v.branch_id = %(b)s"]
        if date_from:
            cond.append("v.created_at >= %(d1)s")
        if date_to:
            cond.append("v.created_at < %(d2)s")
        if author:
            cond.append("v.author_id = %(a)s")
        if price_type:
            # Фильтр по типу — по массиву изменённых типов в самой версии
            # (достройка: иначе пришлось бы сравнивать JSON строк с родителями).
            cond.append("%(t)s = ANY(v.price_type_ids)")
        sql = f"""
-- Список версий: обход индекса (branch_id, created_at) от новых к старым,
-- фильтры проверяются на лету, пока не наберётся страница.
SELECT v.id, v.created_at, v.author_id, v.status, v.planned_at
FROM x_version v
WHERE {' AND '.join(cond)}
ORDER BY v.created_at DESC
LIMIT %(lim)s
"""
        return Op([Step("q", sql, {"b": branch, "d1": date_from, "d2": date_to, "a": author,
                                   "t": price_type, "lim": limit})])

    def next_change(self, service_id, price_type_id, version_id, is_main):
        sql = f"""
-- Следующее изменение цены одного типа после версии V.
WITH v AS (SELECT last_change_at AS t FROM x_version WHERE id = %(vid)s),
-- значение этого типа на момент V (формула среза)
base AS (SELECT {val("sl", is_main)} AS val FROM v, LATERAL ({SLICE.format(svc="%(s)s", t="v.t")}) sl),
nxt AS (
  -- идём по более поздним строкам услуги (индекс service_id, valid_from) и
  -- берём первую, где значение именно этого типа отличается: строки, созданные
  -- изменениями других типов, читаются и отбрасываются сравнением JSON
  SELECT r.id, r.valid_from, {val("r", is_main)} AS value
  FROM x_row r, v, base
  WHERE r.service_id = %(s)s AND r.valid_from > v.t
    -- непривязанная строка с будущим valid_from ещё не применена — не изменение
    AND NOT (r.child_id IS NULL AND r.valid_from > now())
    AND {val("r", is_main)} IS DISTINCT FROM base.val
  ORDER BY r.valid_from LIMIT 1
)
SELECT nxt.*, ver.id AS version_id, ver.created_at, ver.author_id
-- версия найденной строки: связь в X только версия -> строки, поэтому ищем
-- обратным ходом — ближайшую по времени версию филиала, в row_ids которой есть id
FROM nxt LEFT JOIN LATERAL (
  SELECT xv.id, xv.created_at, xv.author_id FROM x_version xv
  WHERE xv.branch_id = (SELECT branch_id FROM service WHERE id = %(s)s)
    AND xv.created_at <= nxt.valid_from AND nxt.id = ANY(xv.row_ids)
  ORDER BY xv.created_at DESC LIMIT 1
) ver ON true
"""
        return Op([Step("q", sql, {"s": service_id, "key": str(price_type_id), "vid": version_id})])

    # ------------------------------------------------------------- запись
    def mass_change(self, branch, author, price_type_id, is_main, pct, planned_at):
        p = {"b": branch, "a": author, "key": str(price_type_id), "tid": price_type_id, "p": pct}
        new = f"round({val('cur', is_main)} * (100 + %(p)s) / 100.0, 2)"
        svc = "s.branch_id = %(b)s AND NOT s.archived"
        if planned_at is None:
            # Сразу: в дневную версию, через общий write_one_type.
            return Op([
                Step("ver", OPEN_DAILY, p),
                Step("rows", write_one_type(svc, new, is_main, only_filled=True), {**p, "vid": Ref("ver", "id")}),
            ], explain=1)
        p_new, o_new = set_cols("t", is_main, "t.new_value")
        ver = """
-- Запланированная версия: срез по ней берётся на момент planned_at.
INSERT INTO x_version (branch_id, author_id, created_at, day, status, planned_at, last_change_at, row_ids, price_type_ids)
VALUES (%(b)s, %(a)s, now(), current_date, 'S', %(at)s, %(at)s, '{}', ARRAY[%(tid)s::int]) RETURNING id
"""
        rows = f"""
-- Запланированное изменение: строки-снимки с будущим valid_from, к цепочке
-- не привязаны (child текущей остаётся NULL) — горячий путь их не видит.
WITH cur AS (
  SELECT r.id, r.service_id, r.price, r.price_overrides FROM x_row r JOIN service s ON s.id = r.service_id
  WHERE {svc} AND {CURRENT} AND {filled("r", is_main)}
), sch AS (
  SELECT q.service_id, q.price, q.price_overrides FROM x_row q JOIN cur ON cur.service_id = q.service_id
  WHERE q.child_id IS NULL AND q.valid_from > now()
), t AS (
  SELECT cur.*, {new} AS new_value FROM cur
  -- этот тип уже запланирован другой версией -> пропускаем услугу
  WHERE NOT EXISTS (SELECT 1 FROM sch WHERE sch.service_id = cur.service_id
                    AND {val("sch", is_main)} IS DISTINCT FROM {val("cur", is_main)})
), ins AS (
  -- снимок = текущая строка с изменённым значением одного типа
  INSERT INTO x_row (service_id, valid_from, price, price_overrides)
  SELECT t.service_id, %(at)s, {p_new}, {o_new} FROM t
  RETURNING id
)
-- версия знает свои строки только через массив id
UPDATE x_version SET row_ids = ARRAY(SELECT id FROM ins) WHERE id = %(vid)s
"""
        return Op([Step("ver", ver, {**p, "at": planned_at}),
                   Step("rows", rows, {**p, "at": planned_at, "vid": Ref("ver", "id")})], explain=1)

    def apply_scheduled(self, branch, version_id):
        sql = """
-- Применение запланированной версии при наступлении даты.
WITH v AS (SELECT row_ids, price_type_ids FROM x_version WHERE id = %(vid)s),
-- ключи JSON изменённых версией типов и признак «менялась основная»
keys AS (SELECT array_agg(pt::text) AS k, bool_or(pt = %(main)s) AS main_changed
         FROM v, unnest(v.price_type_ids) pt),
-- строки версии (массив row_ids разворачивается в набор id)
new AS (SELECT r.id, r.service_id, r.valid_from, r.price, r.price_overrides
        FROM x_row r WHERE r.id IN (SELECT unnest(row_ids) FROM v)),
link AS (
  -- текущая строка услуги -> child = запланированная: та становится хвостом
  UPDATE x_row cur SET child_id = new.id FROM new
  WHERE cur.service_id = new.service_id AND cur.child_id IS NULL
    AND cur.valid_from <= least(now(), new.valid_from) AND cur.id <> new.id
  RETURNING 1
), rebase AS (
  -- другие ещё не применённые строки тех же услуг (более поздние планы) —
  -- тоже снимки всех типов: вписываем в них значения, изменённые этой версией
  UPDATE x_row q SET
    price = CASE WHEN keys.main_changed THEN new.price ELSE q.price END,
    price_overrides = (q.price_overrides - keys.k) || (
      SELECT coalesce(jsonb_object_agg(e.key, e.value), '{}') FROM jsonb_each(new.price_overrides) e
      WHERE e.key = ANY(keys.k))
  FROM new, keys
  WHERE q.service_id = new.service_id AND q.child_id IS NULL AND q.valid_from > new.valid_from
  RETURNING 1
), demote AS (
  UPDATE x_version SET status = 'O' WHERE branch_id = %(b)s AND status = 'A' RETURNING 1
)
-- (SELECT count(*) FROM demote): основной оператор ссылается на demote, поэтому
-- Postgres выполняет его до конца раньше вставки. Без ссылки CTE выполнился бы
-- в конце оператора, и новая 'A' нарушила бы UNIQUE (branch_id) WHERE status='A'.
UPDATE x_version SET status = 'A' WHERE id = %(vid)s AND (SELECT count(*) FROM demote) >= 0
RETURNING (SELECT count(*) FROM link) AS linked, (SELECT count(*) FROM rebase) AS rebased
"""
        return Op([Step("apply", sql, {"b": branch, "vid": version_id, "main": self.main_type})])

    def cancel_scheduled(self, branch, version_id):
        p = {"vid": version_id}
        return Op([
            # Строки запланированной версии не привязаны к цепочкам — удаляем по row_ids.
            Step("rows", "DELETE FROM x_row r USING (SELECT unnest(row_ids) AS id FROM x_version "
                         "WHERE id = %(vid)s AND status = 'S') d WHERE r.id = d.id", p),
            Step("ver", "DELETE FROM x_version WHERE id = %(vid)s AND status = 'S'", p),
        ])

    def point_edit(self, branch, author, edit: Edit, main_type):
        # Одна услуга, один тип — тот же write_one_type, что и массовое изменение.
        is_main = edit.price_type_id == main_type
        p = {"b": branch, "a": author, "key": str(edit.price_type_id), "tid": edit.price_type_id,
             "s": edit.service_id, "val": edit.value}
        return Op([
            Step("ver", OPEN_DAILY, p),
            Step("rows", write_one_type("s.id = %(s)s", "%(val)s::numeric", is_main, only_filled=False),
                 {**p, "vid": Ref("ver", "id")}),
        ], explain=1)

    def restore(self, branch, author, version_id, type_ids):
        # price_type_ids версии восстановления = все типы филиала: какие именно
        # изменились, не считаем (это diff JSON по всем услугам).
        t = f"""
-- Восстановление версии V: новая строка только для услуг, где снимок на V != текущему.
WITH v AS (SELECT last_change_at AS t FROM x_version WHERE id = %(src)s),
cur AS (
  -- текущие снимки всех услуг филиала
  SELECT r.id, r.service_id, r.price, r.price_overrides FROM x_row r JOIN service s ON s.id = r.service_id
  WHERE s.branch_id = %(b)s AND {CURRENT}
), old AS (
  -- снимки на момент V для всех услуг разом (формула среза, set-based)
  SELECT r.service_id, r.price, r.price_overrides
  FROM x_row r JOIN service s ON s.id = r.service_id LEFT JOIN x_row c ON c.id = r.child_id, v
  WHERE s.branch_id = %(b)s AND r.valid_from <= v.t AND (c.id IS NULL OR c.valid_from > v.t)
), t AS (
  -- снимок целиком сравнивается одним выражением: все типы за раз
  SELECT cur.id, cur.service_id, old.price AS new_price, old.price_overrides AS new_overrides
  FROM cur JOIN old ON old.service_id = cur.service_id
  WHERE (cur.price, cur.price_overrides) IS DISTINCT FROM (old.price, old.price_overrides)
)"""
        p = {"b": branch, "a": author, "src": version_id, "restored": version_id, "tids": type_ids}
        # Порядок: сначала отмена запланированных, затем новая версия и снимки.
        steps = [Step(n, q, p) for n, q in CANCEL_ALL]
        steps += [Step("ver", NEW_VERSION, p), Step("rows", write_snapshots(t), {**p, "vid": Ref("ver", "id")})]
        return Op(steps, explain=len(steps) - 1)

    def add_price_type(self, branch, author, main_type, pct_of_main):
        # Новый тип = новый ключ JSON: write_one_type копирует снимок каждой
        # услуги с добавленным ключом (и вписывает его в запланированные снимки).
        p = {"b": branch, "a": author, "pm": 100 + pct_of_main}
        new = "round(cur.price * %(pm)s / 100.0, 2)"
        return Op([
            Step(ADD_TYPE.name, ADD_TYPE.sql, p),
            Step("ver", OPEN_DAILY, p),
            Step("rows", write_one_type("s.branch_id = %(b)s AND NOT s.archived", new, False, only_filled=False),
                 {**p, "key": Ref("ptype", "key"), "tid": Ref("ptype", "id"), "vid": Ref("ver", "id")}),
        ], explain=2)

    def copy_branch(self, src, dst, author):
        t = f"""
-- Применение прайса A к филиалу B. tm — сопоставление типов A -> B по порядку.
WITH {TYPE_MAP},
a AS (
  -- актуальный снимок услуги A с ключами JSON, пересчитанными в id типов B:
  -- ключи — id типов, а они у каждого филиала свои
  SELECT m.b_id AS service_id, r.price,
    (SELECT coalesce(jsonb_object_agg(tm.b_t::text, e.value), '{{}}')
     FROM jsonb_each(r.price_overrides) e JOIN tm ON tm.a_t::text = e.key) AS overrides
  FROM x_row r JOIN service_map m ON m.a_id = r.service_id
  WHERE {CURRENT}
), cur AS (
  -- актуальные снимки B; услуги с запланированной строкой пропускаем целиком
  -- (упрощение: в датасете у B запланированных нет)
  SELECT r.id, r.service_id, r.price, r.price_overrides FROM x_row r JOIN service s ON s.id = r.service_id
  WHERE s.branch_id = %(dst)s AND {CURRENT}
    AND NOT EXISTS (SELECT 1 FROM x_row q WHERE q.service_id = r.service_id
                    AND q.child_id IS NULL AND q.valid_from > now())
), t AS (
  -- новая строка только там, где снимок A != снимку B (все типы одним сравнением)
  SELECT cur.id, cur.service_id, a.price AS new_price, a.overrides AS new_overrides
  FROM cur JOIN a ON a.service_id = cur.service_id
  WHERE (cur.price, cur.price_overrides) IS DISTINCT FROM (a.price, a.overrides)
)"""
        p = {"b": dst, "a": author, "src": src, "dst": dst, "restored": None}
        return Op([Step("ver", NEW_VERSION, p),
                   Step("rows", write_snapshots(t, "ARRAY(SELECT b_t::int FROM tm)"),
                        {**p, "vid": Ref("ver", "id")})], explain=1)

    def import_prices(self, branch, author):
        p = {"b": branch, "a": author, "restored": None}
        rows = """
-- Импорт в пустой прайс: одна строка на услугу, только основная цена, JSON пуст;
-- цепочек ещё нет — привязывать нечего.
WITH ins AS (
  INSERT INTO x_row (service_id, valid_from, price, price_overrides)
  SELECT s.id, now(), st.price, '{}'
  FROM import_staging st JOIN service s ON s.branch_id = %(b)s AND s.code = st.code
  RETURNING id
)
UPDATE x_version SET row_ids = ARRAY(SELECT id FROM ins),
       price_type_ids = ARRAY(SELECT id::int FROM price_type WHERE branch_id = %(b)s AND kind = 'main')
WHERE id = %(vid)s
"""
        steps = [Step(c.name, c.sql, p) for c in IMPORT]
        steps += [Step("ver", NEW_VERSION, p), Step("rows", rows, {**p, "vid": Ref("ver", "id")})]
        return Op(steps, explain=len(steps) - 1)

    main_type: int = 0  # проставляет раннер из контекста датасета
