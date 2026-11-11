"""Модель Y2: Y без обновления старых записей (append-only значения).

Схема: y2_value — одна строка на изменение пары услуга x тип, без status и next_ver.
  ver — номер версии (NULL, пока версия запланирована).
Актуальное значение пары — запись с наибольшим ver: обратный спуск по
  y2_value_latest_uq (service_id, price_type_id, ver) INCLUDE (value) WHERE ver IS NOT NULL
с LIMIT 1. Запись изменения — один INSERT, старые записи не трогаются.
"""
from bench.common_ops import ADD_TYPE, IMPORT, TYPE_MAP
from bench.iface import Edit, Op, PriceModel, Ref, Step

GUARD = ("-- (SELECT count(*) FROM demote): основной оператор ссылается на demote, поэтому\n"
         "-- Postgres выполняет его до конца раньше вставки; без ссылки новая 'A' нарушила бы\n"
         "-- UNIQUE (branch_id) WHERE status='A'.\n")


def latest(svc, typ, alias, upto=None):
    """LATERAL: последняя применённая запись пары (svc, typ), опционально с ver <= upto."""
    cond = f" AND v.ver <= {upto}" if upto else ""
    return f"""LATERAL (
  SELECT v.id, v.value, v.ver, v.version_id FROM y2_value v
  WHERE v.service_id = {svc} AND v.price_type_id = {typ} AND v.ver IS NOT NULL{cond}
  ORDER BY v.ver DESC LIMIT 1
) {alias}"""


OPEN_DAILY = """
-- Открыть дневную версию (филиал, день, сотрудник) или взять открытую — как в Y.
WITH cur AS (
    SELECT id, number FROM y2_version
    WHERE branch_id = %(b)s AND status = 'A' AND author_id = %(a)s
      AND created_at >= date_trunc('day', now())
), demote AS (
    UPDATE y2_version SET status = 'O'
    WHERE branch_id = %(b)s AND status = 'A' AND NOT EXISTS (SELECT 1 FROM cur)
    RETURNING 1
), ins AS (
    """ + GUARD.replace("\n-- ", "\n    -- ") + """    INSERT INTO y2_version (branch_id, number, author_id, created_at, status)
    SELECT %(b)s, nextval('y2_version_number_seq'), %(a)s, now(), 'A'
    WHERE NOT EXISTS (SELECT 1 FROM cur) AND (SELECT count(*) FROM demote) >= 0
    RETURNING id, number
)
SELECT id, number FROM cur UNION ALL SELECT id, number FROM ins
"""

WRITE_VALUES = """
-- Записать новые значения пар в версию vid (номер vnum). Ожидает выше CTE src.
-- Старые записи не трогаем: новая запись с бОльшим ver сама становится актуальной.
, inplace AS (
    -- пара уже менялась в этой же версии: правим её запись на месте
    -- (UNIQUE (service, type, ver) не даст вставить вторую запись в ту же версию)
    UPDATE y2_value v SET value = src.new_value
    FROM src WHERE v.service_id = src.service_id AND v.price_type_id = src.price_type_id AND v.ver = %(vnum)s
    RETURNING v.service_id, v.price_type_id
), ins AS (
    INSERT INTO y2_value (service_id, price_type_id, version_id, ver, value)
    SELECT src.service_id, src.price_type_id, %(vid)s, %(vnum)s, src.new_value FROM src
    WHERE NOT EXISTS (SELECT 1 FROM y2_value v WHERE v.service_id = src.service_id
                      AND v.price_type_id = src.price_type_id AND v.ver = %(vnum)s)
    RETURNING 1
)
SELECT (SELECT count(*) FROM ins) AS inserted, (SELECT count(*) FROM inplace) AS updated_in_place
"""

NEW_VERSION = """
-- Отдельная новая версия (восстановление, применение к филиалу, импорт).
WITH demote AS (
    UPDATE y2_version SET status = 'O' WHERE branch_id = %(b)s AND status = 'A' RETURNING 1
)
""" + GUARD + """INSERT INTO y2_version (branch_id, number, author_id, created_at, status, restored_from_id)
SELECT %(b)s, nextval('y2_version_number_seq'), %(a)s, now(), 'A', %(restored)s
WHERE (SELECT count(*) FROM demote) >= 0
RETURNING id, number
"""

CANCEL_ALL = [
    ("cancel_values", """
-- Отмена всех запланированных версий филиала: их записи (ver IS NULL) удаляем.
DELETE FROM y2_value v USING y2_version ver
WHERE v.version_id = ver.id AND ver.branch_id = %(b)s AND ver.status = 'S'
"""),
    ("cancel_versions", "DELETE FROM y2_version WHERE branch_id = %(b)s AND status = 'S'"),
]

# Актуальные значения всех пар набора услуг (FROM/WHERE подставляются):
# по паре на (услуга x тип филиала) — спуск по y2_value_latest_uq с LIMIT 1.
# Отдельного индекса «только актуальные» нет, поэтому «текущее состояние» —
# это 85 тыс. x T спусков, а не проход по маленькому частичному индексу, как в Y.
CURRENT_ALL = """
  SELECT s.id AS service_id, pt.id AS price_type_id, lv.value, lv.version_id
  FROM service s JOIN price_type pt ON pt.branch_id = s.branch_id {type_filter}
  JOIN {lat} ON true
  WHERE {svc_filter}
"""


def current_all(svc_filter, type_filter=""):
    return CURRENT_ALL.format(svc_filter=svc_filter, type_filter=type_filter,
                              lat=latest("s.id", "pt.id", "lv"))


def page_where(cursor, extra=""):
    cond = "s.branch_id = %(b)s AND NOT s.archived" + extra
    if cursor:
        cond += " AND (s.category_sort, s.code, s.name, s.id) > (%(c1)s, %(c2)s, %(c3)s, %(c4)s)"
    return cond


def cursor_params(cursor):
    return dict(zip(("c1", "c2", "c3", "c4"), cursor)) if cursor else {}


class Y2Model(PriceModel):
    name = "Y2"

    # ------------------------------------------------------------- чтение
    def current_prices(self, service_ids, main_type, ins_type):
        sql = f"""
-- Горячий путь: последняя запись пары — обратный спуск по y2_value_latest_uq,
-- value берётся из INCLUDE (index-only scan).
SELECT s.id, s.code, s.name, m.value AS main_price, i.value AS ins_price
FROM service s
JOIN {latest("s.id", "%(main)s", "m")} ON true
LEFT JOIN {latest("s.id", "%(ins)s", "i")} ON true
WHERE s.id = ANY(%(ids)s)
"""
        return Op([Step("q", sql, {"ids": service_ids, "main": main_type, "ins": ins_type})])

    def price_page(self, branch, cursor, limit, type_ids, missing_type=None):
        extra = ""
        if missing_type:
            # «только без цены»: последняя запись пары отсутствует или пуста
            extra = f""" AND NOT EXISTS (SELECT 1 FROM {latest("s.id", "%(mt)s", "mv")} WHERE mv.value IS NOT NULL)"""
        sql = f"""
-- Страница прайса: для каждой услуги и каждого типа филиала — последняя запись пары.
SELECT s.id, s.category_sort, s.code, s.name,
  (SELECT jsonb_object_agg(pt.id, lv.value) FROM price_type pt JOIN {latest("s.id", "pt.id", "lv")} ON true
    WHERE pt.branch_id = s.branch_id AND lv.value IS NOT NULL) AS prices,
  (SELECT jsonb_object_agg(v.price_type_id, jsonb_build_object('value', v.value, 'at', ver.planned_at))
    FROM y2_value v JOIN y2_version ver ON ver.id = v.version_id
    WHERE v.service_id = s.id AND v.ver IS NULL) AS scheduled
FROM service s
WHERE {page_where(cursor, extra)}
ORDER BY s.category_sort, s.code, s.name, s.id
LIMIT %(lim)s
"""
        return Op([Step("q", sql, {"b": branch, "lim": limit, "mt": missing_type, **cursor_params(cursor)})])

    def version_page(self, branch, version_id, cursor, limit, type_ids):
        sql = f"""
-- «Прайс-лист от версии»: срез на номер N — как в Y (последняя запись с ver <= N).
WITH vn AS (SELECT number AS n FROM y2_version WHERE id = %(vid)s),
page AS (
  SELECT s.id, s.category_sort, s.code, s.name FROM service s
  WHERE {page_where(cursor)}
  ORDER BY s.category_sort, s.code, s.name, s.id LIMIT %(lim)s
)
SELECT p.id, p.code, p.name, pt.id AS price_type_id, cur.value, cur.ver = vn.n AS changed,
       prev.value AS old_value
FROM page p CROSS JOIN vn
CROSS JOIN unnest(%(types)s::bigint[]) AS pt(id)
JOIN {latest("p.id", "pt.id", "cur", upto="vn.n")} ON true
-- старая цена: предыдущая запись пары (ver < N) — ещё один спуск по тому же
-- индексу вместо перехода по next_ver; только для изменённых в версии пар
LEFT JOIN LATERAL (
  SELECT v.value FROM y2_value v
  WHERE cur.ver = vn.n AND v.service_id = p.id AND v.price_type_id = pt.id
    AND v.ver IS NOT NULL AND v.ver < vn.n
  ORDER BY v.ver DESC LIMIT 1
) prev ON true
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
            cond.append("EXISTS (SELECT 1 FROM y2_value x WHERE x.version_id = v.id AND x.price_type_id = %(t)s)")
        sql = f"""
-- Список версий — как в Y.
SELECT v.id, v.number, v.created_at, v.author_id, v.status, v.planned_at
FROM y2_version v
WHERE {' AND '.join(cond)}
ORDER BY v.created_at DESC
LIMIT %(lim)s
"""
        return Op([Step("q", sql, {"b": branch, "d1": date_from, "d2": date_to, "a": author,
                                   "t": price_type, "lim": limit})])

    def next_change(self, service_id, price_type_id, version_id, is_main):
        sql = """
-- Следующее изменение пары после версии V: первая запись пары с ver > V — как в Y.
SELECT v.ver, v.value, ver.created_at, ver.author_id
FROM y2_value v JOIN y2_version ver ON ver.id = v.version_id
WHERE v.service_id = %(s)s AND v.price_type_id = %(t)s
  AND v.ver > (SELECT number FROM y2_version WHERE id = %(vid)s)
ORDER BY v.ver LIMIT 1
"""
        return Op([Step("q", sql, {"s": service_id, "t": price_type_id, "vid": version_id})])

    # ------------------------------------------------------------- запись
    def mass_change(self, branch, author, price_type_id, is_main, pct, planned_at):
        src = f"""
-- Пары для массового изменения: актуальные непустые цены типа t в филиале
-- (85 тыс. спусков по индексу последних записей), кроме запланированных.
WITH src AS (
  SELECT c.service_id, c.price_type_id, round(c.value * (100 + %(p)s) / 100.0, 2) AS new_value
  FROM ({current_all("s.branch_id = %(b)s AND NOT s.archived", "AND pt.id = %(t)s")}) c
  WHERE c.value IS NOT NULL
    AND NOT EXISTS (SELECT 1 FROM y2_value q WHERE q.service_id = c.service_id
                    AND q.price_type_id = c.price_type_id AND q.ver IS NULL)
)"""
        p = {"b": branch, "a": author, "t": price_type_id, "p": pct}
        if planned_at is None:
            return Op([
                Step("ver", OPEN_DAILY, p),
                Step("values", src + WRITE_VALUES, {**p, "vid": Ref("ver", "id"), "vnum": Ref("ver", "number")}),
            ], explain=1)
        ver = """
INSERT INTO y2_version (branch_id, number, author_id, created_at, status, planned_at)
VALUES (%(b)s, NULL, %(a)s, now(), 'S', %(at)s) RETURNING id
"""
        vals = src + """
-- Запланированные записи: ver = NULL до применения, актуальные не трогаем.
INSERT INTO y2_value (service_id, price_type_id, version_id, ver, value)
SELECT service_id, price_type_id, %(vid)s, NULL, new_value FROM src
"""
        return Op([Step("ver", ver, {**p, "at": planned_at}),
                   Step("values", vals, {**p, "vid": Ref("ver", "id")})], explain=1)

    def apply_scheduled(self, branch, version_id):
        ver = """
-- Применение: версия получает номер и становится последней в филиале.
WITH demote AS (
  UPDATE y2_version SET status = 'O' WHERE branch_id = %(b)s AND status = 'A' RETURNING 1
)
""" + GUARD + """UPDATE y2_version SET number = nextval('y2_version_number_seq'), status = 'A'
WHERE id = %(vid)s AND (SELECT count(*) FROM demote) >= 0 RETURNING number
"""
        new = """
-- Единственная запись данных: запланированным записям проставляется номер.
-- Старые актуальные не трогаются — новая запись с бОльшим ver сама становится последней.
UPDATE y2_value SET ver = %(n)s WHERE version_id = %(vid)s
"""
        p = {"b": branch, "vid": version_id}
        return Op([Step("ver", ver, p), Step("new", new, {**p, "n": Ref("ver", "number")})], explain=1)

    def cancel_scheduled(self, branch, version_id):
        p = {"vid": version_id}
        return Op([
            Step("values", "DELETE FROM y2_value WHERE version_id = %(vid)s", p),
            Step("ver", "DELETE FROM y2_version WHERE id = %(vid)s AND status = 'S'", p),
        ])

    def point_edit(self, branch, author, edit: Edit, main_type):
        src = """
-- Одна пара; если по ней висит запланированное изменение — src пуст, правки нет.
WITH src AS (
  SELECT %(s)s::bigint AS service_id, %(t)s::bigint AS price_type_id, %(val)s::numeric AS new_value
  WHERE NOT EXISTS (SELECT 1 FROM y2_value q WHERE q.service_id = %(s)s
                    AND q.price_type_id = %(t)s AND q.ver IS NULL)
)"""
        p = {"b": branch, "a": author, "s": edit.service_id, "t": edit.price_type_id, "val": edit.value}
        return Op([
            Step("ver", OPEN_DAILY, p),
            Step("values", src + WRITE_VALUES, {**p, "vid": Ref("ver", "id"), "vnum": Ref("ver", "number")}),
        ], explain=1)

    def restore(self, branch, author, version_id, type_ids):
        src = """
-- Восстановление версии V: только пары, где значение на V != актуальному.
-- И «актуальное», и «на V» — последняя запись пары (с ver <= N для V): один
-- проход DISTINCT ON по истории филиала даёт оба значения сразу.
WITH n AS (SELECT number AS n FROM y2_version WHERE id = %(src)s),
h AS (
  SELECT v.service_id, v.price_type_id, v.ver, v.value
  FROM y2_value v JOIN service s ON s.id = v.service_id
  WHERE s.branch_id = %(b)s AND v.ver IS NOT NULL
), cur AS (
  SELECT DISTINCT ON (service_id, price_type_id) service_id, price_type_id, value FROM h
  ORDER BY service_id DESC, price_type_id DESC, ver DESC
), old AS (
  SELECT DISTINCT ON (h.service_id, h.price_type_id) h.service_id, h.price_type_id, h.value FROM h, n
  WHERE h.ver <= n.n
  ORDER BY h.service_id DESC, h.price_type_id DESC, h.ver DESC
), src AS (
  SELECT coalesce(cur.service_id, old.service_id) AS service_id,
         coalesce(cur.price_type_id, old.price_type_id) AS price_type_id, old.value AS new_value
  FROM cur FULL JOIN old ON old.service_id = cur.service_id AND old.price_type_id = cur.price_type_id
  WHERE cur.value IS DISTINCT FROM old.value
)"""
        p = {"b": branch, "a": author, "src": version_id, "restored": version_id}
        steps = [Step(n, q, p) for n, q in CANCEL_ALL]
        steps += [Step("ver", NEW_VERSION, p),
                  Step("values", src + WRITE_VALUES, {**p, "vid": Ref("ver", "id"), "vnum": Ref("ver", "number")})]
        return Op(steps, explain=len(steps) - 1)

    def add_price_type(self, branch, author, main_type, pct_of_main):
        src = f"""
-- Новый тип: значение = актуальная основная x (100 + pct) / 100; только INSERT.
WITH src AS (
  SELECT c.service_id, %(nt)s::bigint AS price_type_id, round(c.value * %(pm)s / 100.0, 2) AS new_value
  FROM ({current_all("s.branch_id = %(b)s AND NOT s.archived", "AND pt.id = %(main)s")}) c
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
-- Применение прайса A к филиалу B: актуальные пары A и B (спуски по индексу
-- последних записей), пишем только отличающиеся; пары B с планом не трогаем.
WITH {TYPE_MAP},
av AS (
  SELECT m.b_id AS service_id, tm.b_t AS price_type_id, c.value
  FROM ({current_all("s.branch_id = %(src)s")}) c
  JOIN service_map m ON m.a_id = c.service_id JOIN tm ON tm.a_t = c.price_type_id
), bv AS (
  SELECT c.service_id, c.price_type_id, c.value
  FROM ({current_all("s.branch_id = %(dst)s")}) c JOIN tm ON tm.b_t = c.price_type_id
), src AS (
  SELECT coalesce(av.service_id, bv.service_id) AS service_id,
         coalesce(av.price_type_id, bv.price_type_id) AS price_type_id, av.value AS new_value
  FROM av FULL JOIN bv ON bv.service_id = av.service_id AND bv.price_type_id = av.price_type_id
  WHERE av.value IS DISTINCT FROM bv.value
    AND NOT EXISTS (SELECT 1 FROM y2_value q WHERE q.service_id = coalesce(av.service_id, bv.service_id)
                    AND q.price_type_id = coalesce(av.price_type_id, bv.price_type_id) AND q.ver IS NULL)
)"""
        p = {"b": dst, "a": author, "src": src, "dst": dst, "restored": None}
        return Op([Step("ver", NEW_VERSION, p),
                   Step("values", sql + WRITE_VALUES, {**p, "vid": Ref("ver", "id"), "vnum": Ref("ver", "number")})],
                  explain=1)

    def import_prices(self, branch, author):
        p = {"b": branch, "a": author, "restored": None}
        vals = """
-- Импорт в пустой прайс: одна запись основной цены на услугу.
INSERT INTO y2_value (service_id, price_type_id, version_id, ver, value)
SELECT s.id, (SELECT id FROM price_type WHERE branch_id = %(b)s AND kind = 'main'), %(vid)s, %(vnum)s, st.price
FROM import_staging st JOIN service s ON s.branch_id = %(b)s AND s.code = st.code
"""
        steps = [Step(c.name, c.sql, p) for c in IMPORT]
        steps += [Step("ver", NEW_VERSION, p),
                  Step("values", vals, {**p, "vid": Ref("ver", "id"), "vnum": Ref("ver", "number")})]
        return Op(steps, explain=len(steps) - 1)
