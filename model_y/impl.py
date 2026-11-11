"""Модель Y: реализация операций интерфейса сырым SQL.

Напоминание о схеме: y_value — одна строка на изменение пары услуга x тип.
  status: 'A' актуальная, 'O' старая, 'S' запланированная;
  version_id — версия (всегда), ver — её номер (NULL, пока запланирована);
  next_ver_id — следующая запись той же пары.
Индексы, на которые опираются запросы:
  y_value_actual_uq    (service_id, price_type_id) INCLUDE (value) WHERE status='A'
  y_value_scheduled_uq (service_id, price_type_id) WHERE status='S'
  y_value_slice_idx    (service_id, price_type_id, ver)
  y_value_version_idx  (version_id, price_type_id)
  y_value_next_ver_id_key UNIQUE (next_ver_id)
"""
from bench.common_ops import ADD_TYPE, IMPORT, TYPE_MAP
from bench.iface import Edit, Op, PriceModel, Ref, Step

OPEN_DAILY = """
-- Открыть дневную версию (филиал, день, сотрудник) или взять уже открытую.
-- Вливать правки можно только в версию, которая сейчас последняя применённая
-- в филиале (status='A'): иначе запись с меньшим номером появилась бы позже
-- записи с большим, и срез ver <= N показал бы не ту цену.
WITH cur AS (
    -- моя сегодняшняя версия, если она всё ещё последняя
    SELECT id, number FROM y_version
    WHERE branch_id = %(b)s AND status = 'A' AND author_id = %(a)s
      AND created_at >= date_trunc('day', now())
), demote AS (
    -- открываем новую: прежняя последняя версия филиала становится старой
    UPDATE y_version SET status = 'O'
    WHERE branch_id = %(b)s AND status = 'A' AND NOT EXISTS (SELECT 1 FROM cur)
    RETURNING 1
), ins AS (
    -- номер выдаётся сразу: дневная версия применяется в момент создания
    -- (SELECT count(*) FROM demote): основной оператор ссылается на demote, поэтому
    -- Postgres выполняет его до конца раньше вставки. Без ссылки CTE выполнился бы
    -- в конце оператора, и новая 'A' нарушила бы UNIQUE (branch_id) WHERE status='A'.
    INSERT INTO y_version (branch_id, number, author_id, created_at, status)
    SELECT %(b)s, nextval('y_version_number_seq'), %(a)s, now(), 'A'
    WHERE NOT EXISTS (SELECT 1 FROM cur) AND (SELECT count(*) FROM demote) >= 0
    RETURNING id, number
)
-- ровно одна строка: либо существующая версия, либо новая
SELECT id, number FROM cur UNION ALL SELECT id, number FROM ins
"""

WRITE_VALUES = """
-- Записать новые значения пар в версию vid (номер vnum).
-- Ожидает выше CTE src(service_id, price_type_id, new_value).
, tgt AS (
    -- к каждой паре — её текущая актуальная запись (если есть) и id будущей записи;
    -- id берём из последовательности заранее, чтобы тем же UPDATE проставить
    -- старой записи next_ver_id на новую
    SELECT src.*, a.id AS old_id, a.version_id AS old_version,
           nextval('y_value_id_seq') AS new_id
    FROM src LEFT JOIN y_value a
      ON a.service_id = src.service_id AND a.price_type_id = src.price_type_id AND a.status = 'A'
), inplace AS (
    -- пара уже менялась в этой же версии: правим значение на месте, новой записи нет
    UPDATE y_value v SET value = tgt.new_value
    FROM tgt WHERE v.id = tgt.old_id AND tgt.old_version = %(vid)s
    RETURNING 1
), demote AS (
    -- старая актуальная -> 'O' и ссылка на новую запись, одним UPDATE
    UPDATE y_value v SET status = 'O', next_ver_id = tgt.new_id
    FROM tgt WHERE v.id = tgt.old_id AND tgt.old_version <> %(vid)s
    RETURNING tgt.new_id, tgt.service_id, tgt.price_type_id, tgt.new_value
), ins AS (
    -- новые актуальные записи. Первая ветка читает из RETURNING demote, поэтому
    -- старая запись пары перестаёт быть 'A' раньше, чем вставляется новая:
    -- иначе сработал бы уникальный индекс y_value_actual_uq.
    INSERT INTO y_value (id, service_id, price_type_id, version_id, ver, value, status)
    SELECT new_id, service_id, price_type_id, %(vid)s, %(vnum)s, new_value, 'A' FROM demote
    UNION ALL
    -- вторая ветка: у пары ещё не было ни одной записи (первая цена этого типа)
    SELECT new_id, service_id, price_type_id, %(vid)s, %(vnum)s, new_value, 'A' FROM tgt WHERE old_id IS NULL
    RETURNING 1
)
SELECT (SELECT count(*) FROM ins) AS inserted, (SELECT count(*) FROM inplace) AS updated_in_place
"""

NEW_VERSION = """
-- Отдельная новая версия (восстановление, применение к филиалу, импорт):
-- прежняя последняя версия филиала -> старая, новой сразу выдаётся номер.
WITH demote AS (
    UPDATE y_version SET status = 'O' WHERE branch_id = %(b)s AND status = 'A' RETURNING 1
)
-- (SELECT count(*) FROM demote): основной оператор ссылается на demote, поэтому
-- Postgres выполняет его до конца раньше вставки. Без ссылки CTE выполнился бы
-- в конце оператора, и новая 'A' нарушила бы UNIQUE (branch_id) WHERE status='A'.
INSERT INTO y_version (branch_id, number, author_id, created_at, status, restored_from_id)
SELECT %(b)s, nextval('y_version_number_seq'), %(a)s, now(), 'A', %(restored)s
WHERE (SELECT count(*) FROM demote) >= 0
RETURNING id, number
"""

CANCEL_ALL = [
    ("cancel_values", """
-- Отмена всех запланированных версий филиала: их значения (status='S')
-- ещё ни на что не ссылаются и ни от кого не получали next_ver, удаляем как есть.
DELETE FROM y_value v USING y_version ver
WHERE v.version_id = ver.id AND ver.branch_id = %(b)s AND ver.status = 'S'
"""),
    ("cancel_versions", "DELETE FROM y_version WHERE branch_id = %(b)s AND status = 'S'"),
]


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


class YModel(PriceModel):
    name = "Y"

    # ------------------------------------------------------------- чтение
    def current_prices(self, service_ids, main_type, ins_type):
        sql = """
-- Горячий путь: по одному поиску в частичном индексе актуальных на каждый тип.
-- INCLUDE (value) в индексе даёт index-only scan — в таблицу не ходим.
SELECT s.id, s.code, s.name, m.value AS main_price, i.value AS ins_price
FROM service s
-- основная цена есть всегда (инвариант), поэтому JOIN
JOIN y_value m ON m.service_id = s.id AND m.price_type_id = %(main)s AND m.status = 'A'
-- страховой может не быть, поэтому LEFT JOIN; два JOIN дают одну строку на услугу
LEFT JOIN y_value i ON i.service_id = s.id AND i.price_type_id = %(ins)s AND i.status = 'A'
WHERE s.id = ANY(%(ids)s)
"""
        return Op([Step("q", sql, {"ids": service_ids, "main": main_type, "ins": ins_type})])

    def price_page(self, branch, cursor, limit, type_ids, missing_type=None):
        extra = ""
        if missing_type:
            # «Только без цены»: нет актуальной непустой записи этого типа.
            # Проверяется на лету при обходе индекса услуг, пока не наберётся страница.
            extra = """ AND NOT EXISTS (SELECT 1 FROM y_value v WHERE v.service_id = s.id
                 AND v.price_type_id = %(mt)s AND v.status = 'A' AND v.value IS NOT NULL)"""
        sql = f"""
-- Страница прайса: 50 услуг по индексу service_page_idx, к каждой — два подзапроса.
SELECT s.id, s.category_sort, s.code, s.name,
  -- все актуальные цены услуги в один JSON (тип -> цена); T поисков в индексе актуальных
  (SELECT jsonb_object_agg(v.price_type_id, v.value) FROM y_value v
    WHERE v.service_id = s.id AND v.status = 'A' AND v.value IS NOT NULL) AS prices,
  -- запланированные изменения услуги с датой применения (индекс y_value_scheduled_uq)
  (SELECT jsonb_object_agg(v.price_type_id, jsonb_build_object('value', v.value, 'at', ver.planned_at))
    FROM y_value v JOIN y_version ver ON ver.id = v.version_id
    WHERE v.service_id = s.id AND v.status = 'S') AS scheduled
FROM service s
WHERE {page_where(cursor, extra)}
ORDER BY s.category_sort, s.code, s.name, s.id
LIMIT %(lim)s
"""
        return Op([Step("q", sql, {"b": branch, "lim": limit, "mt": missing_type, **cursor_params(cursor)})])

    def version_page(self, branch, version_id, cursor, limit, type_ids):
        sql = f"""
-- «Прайс-лист от версии»: срез на номер версии N для страницы из 50 услуг.
WITH vn AS (SELECT number AS n FROM y_version WHERE id = %(vid)s),
page AS (
  SELECT s.id, s.category_sort, s.code, s.name FROM service s
  WHERE {page_where(cursor)}
  ORDER BY s.category_sort, s.code, s.name, s.id LIMIT %(lim)s
)
SELECT p.id, p.code, p.name, pt.id AS price_type_id, cur.value,
       -- «изменено в этой версии» — запись пары создана именно версией N
       cur.ver = vn.n AS changed,
       prev.value AS old_value
FROM page p CROSS JOIN vn
-- все типы филиала: 50 услуг x T типов пар
CROSS JOIN unnest(%(types)s::bigint[]) AS pt(id)
-- значение пары на версию N: последняя запись с ver <= N,
-- один спуск по индексу y_value_slice_idx в обратном порядке и LIMIT 1
JOIN LATERAL (
  SELECT v.id, v.value, v.ver FROM y_value v
  WHERE v.service_id = p.id AND v.price_type_id = pt.id AND v.ver <= vn.n
  ORDER BY v.ver DESC LIMIT 1
) cur ON true
-- старая цена относительно версии: предыдущая запись пары — та, у которой
-- next_ver указывает на текущую (уникальный индекс по next_ver_id).
-- Ищем только для изменённых в версии пар.
LEFT JOIN y_value prev ON prev.next_ver_id = cur.id AND cur.ver = vn.n
ORDER BY p.category_sort, p.code, p.name, p.id, pt.id
"""
        return Op([Step("q", sql, {"b": branch, "vid": version_id, "lim": limit, "types": type_ids,
                                   **cursor_params(cursor)})])

    def history(self, branch, date_from, date_to, author, price_type, limit):
        cond = ["v.branch_id = %(b)s"]
        if date_from:
            cond.append("v.created_at >= %(d1)s")
        if date_to:
            cond.append("v.created_at < %(d2)s")
        if author:
            cond.append("v.author_id = %(a)s")
        if price_type:
            # Фильтр по типу: в версии есть хоть одно значение этого типа
            # (индекс y_value_version_idx, одна точечная проверка на версию).
            cond.append("EXISTS (SELECT 1 FROM y_value x WHERE x.version_id = v.id AND x.price_type_id = %(t)s)")
        sql = f"""
-- Список версий: обход индекса (branch_id, created_at) от новых к старым,
-- фильтры проверяются на лету, пока не наберётся страница.
SELECT v.id, v.number, v.created_at, v.author_id, v.status, v.planned_at
FROM y_version v
WHERE {' AND '.join(cond)}
ORDER BY v.created_at DESC
LIMIT %(lim)s
"""
        return Op([Step("q", sql, {"b": branch, "d1": date_from, "d2": date_to, "a": author,
                                   "t": price_type, "lim": limit})])

    def next_change(self, service_id, price_type_id, version_id, is_main):
        sql = """
-- Следующее изменение пары после версии V: первая запись этой пары с ver > V.
-- Один спуск по y_value_slice_idx (service_id, price_type_id, ver); изменения
-- других типов той же услуги лежат в других ветках индекса и не читаются.
SELECT v.ver, v.value, ver.created_at, ver.author_id
FROM y_value v JOIN y_version ver ON ver.id = v.version_id
WHERE v.service_id = %(s)s AND v.price_type_id = %(t)s
  AND v.ver > (SELECT number FROM y_version WHERE id = %(vid)s)
ORDER BY v.ver LIMIT 1
"""
        return Op([Step("q", sql, {"s": service_id, "t": price_type_id, "vid": version_id})])

    # ------------------------------------------------------------- запись
    def mass_change(self, branch, author, price_type_id, is_main, pct, planned_at):
        src = """
-- Пары для массового изменения: все актуальные непустые цены типа t в филиале.
WITH src AS (
  SELECT v.service_id, v.price_type_id, round(v.value * (100 + %(p)s) / 100.0, 2) AS new_value
  FROM y_value v JOIN service s ON s.id = v.service_id
  WHERE s.branch_id = %(b)s AND NOT s.archived
    AND v.price_type_id = %(t)s AND v.status = 'A' AND v.value IS NOT NULL
    -- пары с запланированным изменением менять нельзя — пропускаем
    AND NOT EXISTS (SELECT 1 FROM y_value q WHERE q.service_id = v.service_id
                    AND q.price_type_id = v.price_type_id AND q.status = 'S')
)"""
        p = {"b": branch, "a": author, "t": price_type_id, "p": pct}
        if planned_at is None:
            # Сразу: в дневную версию, через общий WRITE_VALUES.
            return Op([
                Step("ver", OPEN_DAILY, p),
                Step("values", src + WRITE_VALUES, {**p, "vid": Ref("ver", "id"), "vnum": Ref("ver", "number")}),
            ], explain=1)
        ver = """
-- Запланированная версия: номера нет (NULL) до применения.
INSERT INTO y_version (branch_id, number, author_id, created_at, status, planned_at)
VALUES (%(b)s, NULL, %(a)s, now(), 'S', %(at)s) RETURNING id
"""
        vals = src + """
-- Запланированные значения: status='S', ver=NULL; старые актуальные не трогаем —
-- они остаются в силе до применения. Связь с версией — через version_id.
INSERT INTO y_value (service_id, price_type_id, version_id, ver, value, status)
SELECT service_id, price_type_id, %(vid)s, NULL, new_value, 'S' FROM src
"""
        return Op([
            Step("ver", ver, {**p, "at": planned_at}),
            Step("values", vals, {**p, "vid": Ref("ver", "id")}),
        ], explain=1)

    def apply_scheduled(self, branch, version_id):
        ver = """
-- Применение: версия получает следующий номер и становится последней в филиале.
WITH demote AS (
  UPDATE y_version SET status = 'O' WHERE branch_id = %(b)s AND status = 'A' RETURNING 1
)
-- (SELECT count(*) FROM demote): основной оператор ссылается на demote, поэтому
-- Postgres выполняет его до конца раньше вставки. Без ссылки CTE выполнился бы
-- в конце оператора, и новая 'A' нарушила бы UNIQUE (branch_id) WHERE status='A'.
UPDATE y_version SET number = nextval('y_version_number_seq'), status = 'A'
WHERE id = %(vid)s AND (SELECT count(*) FROM demote) >= 0 RETURNING number
"""
        old = """
-- Шаг 1: старые актуальные записи тех же пар -> 'O' и next_ver на запланированную.
-- Значения версии находятся по y_value_version_idx, пары — по индексу актуальных.
UPDATE y_value a SET status = 'O', next_ver_id = s.id
FROM y_value s
WHERE s.version_id = %(vid)s
  AND a.service_id = s.service_id AND a.price_type_id = s.price_type_id AND a.status = 'A'
"""
        new = """
-- Шаг 2: запланированные -> актуальные с номером версии. Отдельным оператором
-- после шага 1: пока старая запись пары 'A', уникальный индекс актуальных
-- не дал бы сделать 'A' новую.
UPDATE y_value SET status = 'A', ver = %(n)s WHERE version_id = %(vid)s
"""
        p = {"b": branch, "vid": version_id}
        return Op([Step("ver", ver, p), Step("old", old, p), Step("new", new, {**p, "n": Ref("ver", "number")})],
                  explain=1)

    def cancel_scheduled(self, branch, version_id):
        p = {"vid": version_id}
        return Op([
            # Запланированные значения ни на что не повлияли — просто удаляем
            # (поиск по y_value_version_idx), затем саму версию.
            Step("values", "DELETE FROM y_value WHERE version_id = %(vid)s", p),
            Step("ver", "DELETE FROM y_version WHERE id = %(vid)s AND status = 'S'", p),
        ])

    def point_edit(self, branch, author, edit: Edit, main_type):
        src = """
-- Одна пара; если по ней висит запланированное изменение — src пуст, правки нет.
WITH src AS (
  SELECT %(s)s::bigint AS service_id, %(t)s::bigint AS price_type_id, %(val)s::numeric AS new_value
  WHERE NOT EXISTS (SELECT 1 FROM y_value q WHERE q.service_id = %(s)s
                    AND q.price_type_id = %(t)s AND q.status = 'S')
)"""
        p = {"b": branch, "a": author, "s": edit.service_id, "t": edit.price_type_id, "val": edit.value}
        return Op([
            Step("ver", OPEN_DAILY, p),
            Step("values", src + WRITE_VALUES, {**p, "vid": Ref("ver", "id"), "vnum": Ref("ver", "number")}),
        ], explain=1)

    def restore(self, branch, author, version_id, type_ids):
        src = """
-- Восстановление версии V: пишем только пары, где значение на V != актуальному.
WITH n AS (SELECT number AS n FROM y_version WHERE id = %(src)s),
cur AS (
  -- актуальные значения всех пар филиала (индекс актуальных)
  SELECT a.service_id, a.price_type_id, a.value FROM y_value a JOIN service s ON s.id = a.service_id
  WHERE s.branch_id = %(b)s AND a.status = 'A'
), old AS (
  -- срез на V для всех пар разом: последняя запись с ver <= N.
  -- DISTINCT ON по отсортированной истории, а не LATERAL на каждую пару:
  -- для сотен тысяч пар один проход с сортировкой дешевле поисков.
  SELECT DISTINCT ON (v.service_id, v.price_type_id) v.service_id, v.price_type_id, v.value
  FROM y_value v JOIN service s ON s.id = v.service_id, n
  WHERE s.branch_id = %(b)s AND v.ver <= n.n
  ORDER BY v.service_id DESC, v.price_type_id DESC, v.ver DESC
), src AS (
  -- FULL JOIN ловит оба края: пара есть сейчас, но не было на V (-> очистить,
  -- new_value = NULL) и пара была на V, но сейчас её нет (-> вернуть)
  SELECT coalesce(cur.service_id, old.service_id) AS service_id,
         coalesce(cur.price_type_id, old.price_type_id) AS price_type_id, old.value AS new_value
  FROM cur FULL JOIN old ON old.service_id = cur.service_id AND old.price_type_id = cur.price_type_id
  WHERE cur.value IS DISTINCT FROM old.value
)"""
        p = {"b": branch, "a": author, "src": version_id, "restored": version_id}
        # Порядок: сначала отмена запланированных, затем новая версия и значения.
        steps = [Step(n, q, p) for n, q in CANCEL_ALL]
        steps += [Step("ver", NEW_VERSION, p),
                  Step("values", src + WRITE_VALUES, {**p, "vid": Ref("ver", "id"), "vnum": Ref("ver", "number")})]
        return Op(steps, explain=len(steps) - 1)

    def add_price_type(self, branch, author, main_type, pct_of_main):
        src = """
-- Новый тип: значение = основная цена x (100 + pct) / 100 для каждой услуги.
-- У новой пары нет прежних записей — WRITE_VALUES только вставляет.
WITH src AS (
  SELECT a.service_id, %(nt)s::bigint AS price_type_id, round(a.value * %(pm)s / 100.0, 2) AS new_value
  FROM y_value a JOIN service s ON s.id = a.service_id
  WHERE s.branch_id = %(b)s AND NOT s.archived AND a.price_type_id = %(main)s AND a.status = 'A'
)"""
        p = {"b": branch, "a": author, "main": main_type, "pm": 100 + pct_of_main}
        return Op([
            Step(ADD_TYPE.name, ADD_TYPE.sql, p),
            Step("ver", OPEN_DAILY, p),
            Step("values", src + WRITE_VALUES, {**p, "nt": Ref("ptype", "id"), "vid": Ref("ver", "id"),
                                                "vnum": Ref("ver", "number")}),
        ], explain=2)

    def copy_branch(self, src, dst, author):
        sql = f"""
-- Применение прайса A к филиалу B. tm — сопоставление типов A -> B по порядку.
WITH {TYPE_MAP},
av AS (
  -- актуальные цены A, переведённые в услуги и типы B
  SELECT m.b_id AS service_id, tm.b_t AS price_type_id, v.value
  FROM y_value v JOIN service_map m ON m.a_id = v.service_id JOIN tm ON tm.a_t = v.price_type_id
  WHERE v.status = 'A'
), bv AS (
  -- актуальные цены B по сопоставленным типам
  SELECT v.service_id, v.price_type_id, v.value
  FROM y_value v JOIN service s ON s.id = v.service_id JOIN tm ON tm.b_t = v.price_type_id
  WHERE s.branch_id = %(dst)s AND v.status = 'A'
), src AS (
  -- только отличающиеся пары; цена есть у B, но нет у A -> очистить (new_value NULL)
  SELECT coalesce(av.service_id, bv.service_id) AS service_id,
         coalesce(av.price_type_id, bv.price_type_id) AS price_type_id, av.value AS new_value
  FROM av FULL JOIN bv ON bv.service_id = av.service_id AND bv.price_type_id = av.price_type_id
  WHERE av.value IS DISTINCT FROM bv.value
    -- пары B с запланированным изменением не трогаем
    AND NOT EXISTS (SELECT 1 FROM y_value q WHERE q.service_id = coalesce(av.service_id, bv.service_id)
                    AND q.price_type_id = coalesce(av.price_type_id, bv.price_type_id) AND q.status = 'S')
)"""
        p = {"b": dst, "a": author, "src": src, "dst": dst, "restored": None}
        return Op([Step("ver", NEW_VERSION, p),
                   Step("values", sql + WRITE_VALUES, {**p, "vid": Ref("ver", "id"), "vnum": Ref("ver", "number")})],
                  explain=1)

    def import_prices(self, branch, author):
        p = {"b": branch, "a": author, "restored": None}
        vals = """
-- Импорт в пустой прайс: одна актуальная запись основной цены на услугу,
-- старых записей нет — только INSERT.
INSERT INTO y_value (service_id, price_type_id, version_id, ver, value, status)
SELECT s.id, (SELECT id FROM price_type WHERE branch_id = %(b)s AND kind = 'main'), %(vid)s, %(vnum)s, st.price, 'A'
FROM import_staging st JOIN service s ON s.branch_id = %(b)s AND s.code = st.code
"""
        steps = [Step(c.name, c.sql, p) for c in IMPORT]
        steps += [Step("ver", NEW_VERSION, p),
                  Step("values", vals, {**p, "vid": Ref("ver", "id"), "vnum": Ref("ver", "number")})]
        return Op(steps, explain=len(steps) - 1)
