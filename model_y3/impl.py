"""Модель Y3: история как у Y2 (append-only) + таблица актуальных значений y3_current.

Срез, история и отмена устроены как в Y2 — наследуются с заменой таблиц y2_* на y3_*.
Заново написаны операции, где участвует «текущее»: горячий путь и страница читают
y3_current; каждая запись изменения дописывает историю и делает UPSERT в y3_current
(HOT-обновление: value и ver не входят в индекс).
"""
from bench.iface import Edit, Op, PriceModel, Ref, Step
from model_y2 import impl as y2


def to_y3(op: Op) -> Op:
    for s in op.steps:
        s.sql = s.sql.replace("y2_", "y3_")
    return op


OPEN_DAILY = y2.OPEN_DAILY.replace("y2_", "y3_")
NEW_VERSION = y2.NEW_VERSION.replace("y2_", "y3_")
CANCEL_ALL = [(n, q.replace("y2_", "y3_")) for n, q in y2.CANCEL_ALL]

# UPSERT актуального значения пары. ON CONFLICT ... DO UPDATE меняет только value и ver,
# которых нет в индексе, поэтому обновление HOT и индекс y3_current не трогается.
UPSERT_CURRENT = """
  INSERT INTO y3_current (service_id, price_type_id, value, ver)
  SELECT {cols} FROM {src}
  ON CONFLICT (service_id, price_type_id) DO UPDATE SET value = EXCLUDED.value, ver = EXCLUDED.ver
  RETURNING 1"""

WRITE_VALUES = """
-- Записать новые значения пар в версию vid (номер vnum). Ожидает выше CTE src.
, inplace AS (
    -- пара уже менялась в этой же версии: правим запись истории на месте
    UPDATE y3_value v SET value = src.new_value
    FROM src WHERE v.service_id = src.service_id AND v.price_type_id = src.price_type_id AND v.ver = %(vnum)s
    RETURNING 1
), ins AS (
    -- история только дописывается: старые записи не трогаем
    INSERT INTO y3_value (service_id, price_type_id, version_id, ver, value)
    SELECT src.service_id, src.price_type_id, %(vid)s, %(vnum)s, src.new_value FROM src
    WHERE NOT EXISTS (SELECT 1 FROM y3_value v WHERE v.service_id = src.service_id
                      AND v.price_type_id = src.price_type_id AND v.ver = %(vnum)s)
    RETURNING 1
), head AS (
    -- голова: одна строка на пару, обновление на месте (HOT)""" + UPSERT_CURRENT.format(
    cols="service_id, price_type_id, new_value, %(vnum)s", src="src") + """
)
SELECT (SELECT count(*) FROM ins) AS inserted, (SELECT count(*) FROM inplace) AS updated_in_place,
       (SELECT count(*) FROM head) AS current_upserted
"""


class Y3Model(PriceModel):
    name = "Y3"
    _y2 = y2.Y2Model()

    # ------------------------------------------------------------- чтение
    def current_prices(self, service_ids, main_type, ins_type):
        sql = """
-- Горячий путь: по одной строке y3_current на тип (поиск по первичному ключу).
SELECT s.id, s.code, s.name, m.value AS main_price, i.value AS ins_price
FROM service s
JOIN y3_current m ON m.service_id = s.id AND m.price_type_id = %(main)s
LEFT JOIN y3_current i ON i.service_id = s.id AND i.price_type_id = %(ins)s
WHERE s.id = ANY(%(ids)s)
"""
        return Op([Step("q", sql, {"ids": service_ids, "main": main_type, "ins": ins_type})])

    def price_page(self, branch, cursor, limit, type_ids, missing_type=None):
        extra = ""
        if missing_type:
            extra = """ AND NOT EXISTS (SELECT 1 FROM y3_current c WHERE c.service_id = s.id
                 AND c.price_type_id = %(mt)s AND c.value IS NOT NULL)"""
        sql = f"""
-- Страница прайса: актуальные цены — строки y3_current услуги (соседние в первичном
-- ключе), запланированные — записи истории с ver IS NULL.
SELECT s.id, s.category_sort, s.code, s.name,
  (SELECT jsonb_object_agg(c.price_type_id, c.value) FROM y3_current c
    WHERE c.service_id = s.id AND c.value IS NOT NULL) AS prices,
  (SELECT jsonb_object_agg(v.price_type_id, jsonb_build_object('value', v.value, 'at', ver.planned_at))
    FROM y3_value v JOIN y3_version ver ON ver.id = v.version_id
    WHERE v.service_id = s.id AND v.ver IS NULL) AS scheduled
FROM service s
WHERE {y2.page_where(cursor, extra)}
ORDER BY s.category_sort, s.code, s.name, s.id
LIMIT %(lim)s
"""
        return Op([Step("q", sql, {"b": branch, "lim": limit, "mt": missing_type, **y2.cursor_params(cursor)})])

    # срез, история и следующее изменение — по истории, как в Y2
    def version_page(self, *a, **k):
        return to_y3(self._y2.version_page(*a, **k))

    def history(self, *a, **k):
        return to_y3(self._y2.history(*a, **k))

    def next_change(self, *a, **k):
        return to_y3(self._y2.next_change(*a, **k))

    # ------------------------------------------------------------- запись
    def mass_change(self, branch, author, price_type_id, is_main, pct, planned_at):
        src = """
-- Пары для массового изменения — прямо из y3_current (одна строка на пару).
WITH src AS (
  SELECT c.service_id, c.price_type_id, round(c.value * (100 + %(p)s) / 100.0, 2) AS new_value
  FROM y3_current c JOIN service s ON s.id = c.service_id
  WHERE s.branch_id = %(b)s AND NOT s.archived AND c.price_type_id = %(t)s AND c.value IS NOT NULL
    AND NOT EXISTS (SELECT 1 FROM y3_value q WHERE q.service_id = c.service_id
                    AND q.price_type_id = c.price_type_id AND q.ver IS NULL)
)"""
        p = {"b": branch, "a": author, "t": price_type_id, "p": pct}
        if planned_at is None:
            return Op([Step("ver", OPEN_DAILY, p),
                       Step("values", src + WRITE_VALUES, {**p, "vid": Ref("ver", "id"), "vnum": Ref("ver", "number")})],
                      explain=1)
        ver = """
INSERT INTO y3_version (branch_id, number, author_id, created_at, status, planned_at)
VALUES (%(b)s, NULL, %(a)s, now(), 'S', %(at)s) RETURNING id
"""
        vals = src + """
-- Запланированные записи только в истории (ver = NULL); голова не меняется до применения.
INSERT INTO y3_value (service_id, price_type_id, version_id, ver, value)
SELECT service_id, price_type_id, %(vid)s, NULL, new_value FROM src
"""
        return Op([Step("ver", ver, {**p, "at": planned_at}),
                   Step("values", vals, {**p, "vid": Ref("ver", "id")})], explain=1)

    def apply_scheduled(self, branch, version_id):
        ver = to_y3(self._y2.apply_scheduled(branch, version_id)).steps[0]
        new = """
-- Применение: записям истории проставляется номер, голова получает их значения.
WITH h AS (
  UPDATE y3_value SET ver = %(n)s WHERE version_id = %(vid)s
  RETURNING service_id, price_type_id, value
)""" + UPSERT_CURRENT.format(cols="service_id, price_type_id, value, %(n)s", src="h") + "\n"
        p = {"b": branch, "vid": version_id}
        return Op([ver, Step("new", new, {**p, "n": Ref("ver", "number")})], explain=1)

    def cancel_scheduled(self, branch, version_id):
        # запланированные записи в голову не попадали — удаляем только из истории
        return to_y3(self._y2.cancel_scheduled(branch, version_id))

    def point_edit(self, branch, author, edit: Edit, main_type):
        src = """
WITH src AS (
  SELECT %(s)s::bigint AS service_id, %(t)s::bigint AS price_type_id, %(val)s::numeric AS new_value
  WHERE NOT EXISTS (SELECT 1 FROM y3_value q WHERE q.service_id = %(s)s
                    AND q.price_type_id = %(t)s AND q.ver IS NULL)
)"""
        p = {"b": branch, "a": author, "s": edit.service_id, "t": edit.price_type_id, "val": edit.value}
        return Op([Step("ver", OPEN_DAILY, p),
                   Step("values", src + WRITE_VALUES, {**p, "vid": Ref("ver", "id"), "vnum": Ref("ver", "number")})],
                  explain=1)

    def restore(self, branch, author, version_id, type_ids):
        src = """
-- Восстановление версии V: актуальное — из головы (одна строка на пару),
-- значение на V — DISTINCT ON по истории с ver <= N (как в Y и Y2).
WITH n AS (SELECT number AS n FROM y3_version WHERE id = %(src)s),
cur AS (
  SELECT c.service_id, c.price_type_id, c.value FROM y3_current c JOIN service s ON s.id = c.service_id
  WHERE s.branch_id = %(b)s
), old AS (
  SELECT DISTINCT ON (v.service_id, v.price_type_id) v.service_id, v.price_type_id, v.value
  FROM y3_value v JOIN service s ON s.id = v.service_id, n
  WHERE s.branch_id = %(b)s AND v.ver <= n.n
  ORDER BY v.service_id DESC, v.price_type_id DESC, v.ver DESC
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

    # сц.10–12 в раунде Y3 не замеряются
    def add_price_type(self, *a, **k):
        raise NotImplementedError("Y3: сц.10 не реализован")

    def copy_branch(self, *a, **k):
        raise NotImplementedError("Y3: сц.11 не реализован")

    def import_prices(self, *a, **k):
        raise NotImplementedError("Y3: сц.12 не реализован")
