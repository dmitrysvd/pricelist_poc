"""Модель X, вариант (б): запланированная строка сразу привязана к цепочке.

Цепочка услуги всегда упорядочена по valid_from, хвост может быть в будущем.
Актуальная строка = последняя с valid_from <= now() (по индексу
(service_id, valid_from) — один шаг индекса), применение по дате ничего не пишет
в x_row, только статус версии. Цена — вставка в середину цепочки при
планировании, если у услуги уже есть более поздняя запланированная строка.

Замеряется отдельными строками в сц. 1 и 4 (по просьбе лидера); остальные
операции наследуются от варианта (а).
"""
from bench.iface import Op, Ref, Step
from model_x.impl import XModel, filled, set_cols, val

# Актуальная строка услуги на now(): последняя по valid_from не в будущем.
CURRENT_B = """(SELECT r.* FROM x_row r WHERE r.service_id = {svc} AND r.valid_from <= now()
   ORDER BY r.valid_from DESC LIMIT 1)"""


class XModelB(XModel):
    name = "X(б)"
    only_scenarios = {"1", "4"}

    def current_prices(self, service_ids, main_type, ins_type):
        sql = f"""
SELECT s.id, s.code, s.name, r.price AS main_price, (r.price_overrides ->> %(ins)s)::numeric AS ins_price
FROM service s
JOIN LATERAL {CURRENT_B.format(svc="s.id")} r ON true
WHERE s.id = ANY(%(ids)s)
"""
        return Op([Step("q", sql, {"ids": service_ids, "ins": str(ins_type) if ins_type else None})])

    def mass_change(self, branch, author, price_type_id, is_main, pct, planned_at):
        if planned_at is None:
            return super().mass_change(branch, author, price_type_id, is_main, pct, planned_at)
        # Новая строка встаёт после актуальной: cur.child = new, new.child = прежний
        # cur.child (более поздняя запланированная строка, если есть). Её снимок
        # получает новое значение типа (rebase). UPDATE cur идёт раньше INSERT
        # (INSERT читает из его RETURNING), иначе new.child = old_child нарушил бы
        # UNIQUE(child_id), пока cur.child ещё указывает на old_child.
        p = {"b": branch, "a": author, "key": str(price_type_id), "tid": price_type_id, "p": pct}
        new = f"round({val('cur', is_main)} * (100 + %(p)s) / 100.0, 2)"
        p_new, o_new = set_cols("t", is_main, "t.new_value")
        p_sch, o_sch = set_cols("q", is_main, "t.new_value")
        ver = """
INSERT INTO x_version (branch_id, author_id, created_at, day, status, planned_at, last_change_at, row_ids, price_type_ids)
VALUES (%(b)s, %(a)s, now(), current_date, 'S', %(at)s, %(at)s, '{}', ARRAY[%(tid)s::int]) RETURNING id
"""
        rows = f"""
WITH cur AS (
  SELECT r.id, r.service_id, r.price, r.price_overrides, r.child_id AS old_child
  FROM service s JOIN LATERAL {CURRENT_B.format(svc="s.id")} r ON true
  WHERE s.branch_id = %(b)s AND NOT s.archived AND {filled("r", is_main)}
), sch AS (
  SELECT q.id, q.service_id, q.valid_from, q.price, q.price_overrides FROM x_row q JOIN cur ON q.id = cur.old_child
), t AS (
  SELECT cur.*, {new} AS new_value, nextval('x_row_id_seq') AS new_id FROM cur
  WHERE NOT EXISTS (SELECT 1 FROM sch WHERE sch.service_id = cur.service_id
                    AND {val("sch", is_main)} IS DISTINCT FROM {val("cur", is_main)})
    AND NOT EXISTS (SELECT 1 FROM sch WHERE sch.service_id = cur.service_id AND sch.valid_from <= %(at)s)
), link AS (
  UPDATE x_row r SET child_id = t.new_id FROM t WHERE r.id = t.id
  RETURNING t.*
), ins AS (
  INSERT INTO x_row (id, service_id, valid_from, price, price_overrides, child_id)
  SELECT t.new_id, t.service_id, %(at)s, {p_new}, {o_new}, t.old_child FROM link t
  RETURNING id
), rebase AS (
  UPDATE x_row q SET price = {p_sch}, price_overrides = {o_sch}
  FROM sch JOIN t ON t.service_id = sch.service_id WHERE q.id = sch.id
)
UPDATE x_version SET row_ids = ARRAY(SELECT id FROM ins) WHERE id = %(vid)s
"""
        return Op([Step("ver", ver, {**p, "at": planned_at}),
                   Step("rows", rows, {**p, "at": planned_at, "vid": Ref("ver", "id")})], explain=1)

    def apply_scheduled(self, branch, version_id):
        # Строки уже в цепочке и становятся актуальными сами при наступлении
        # valid_from; меняются только статусы версий.
        sql = """
WITH demote AS (
  UPDATE x_version SET status = 'O' WHERE branch_id = %(b)s AND status = 'A' RETURNING 1
)
-- (SELECT count(*) FROM demote): основной оператор ссылается на demote, поэтому
-- Postgres выполняет его до конца раньше вставки. Без ссылки CTE выполнился бы
-- в конце оператора, и новая 'A' нарушила бы UNIQUE (branch_id) WHERE status='A'.
UPDATE x_version SET status = 'A' WHERE id = %(vid)s AND (SELECT count(*) FROM demote) >= 0
"""
        return Op([Step("apply", sql, {"b": branch, "vid": version_id})])

    def cancel_scheduled(self, branch, version_id):
        # Вынуть строки из середины цепочки: parent.child = row.child, затем DELETE.
        # Сначала обнуляем child удаляемых (иначе UNIQUE(child_id) при перепривязке).
        p = {"vid": version_id}
        return Op([
            Step("collect", """
CREATE TEMP TABLE cancel_rows ON COMMIT DROP AS
SELECT r.id, r.child_id FROM x_row r
WHERE r.id IN (SELECT unnest(row_ids) FROM x_version WHERE id = %(vid)s AND status = 'S')
""", p),
            Step("detach", "UPDATE x_row r SET child_id = NULL FROM cancel_rows c WHERE r.id = c.id", p),
            Step("relink", """
UPDATE x_row par SET child_id = c.child_id FROM cancel_rows c WHERE par.child_id = c.id
""", p),
            Step("rows", "DELETE FROM x_row r USING cancel_rows c WHERE r.id = c.id", p),
            Step("ver", "DELETE FROM x_version WHERE id = %(vid)s AND status = 'S'", p),
        ], explain=3)


class XModelLat(XModel):
    """Вариант среза X для сц. 7: вместо формулы через child — последняя строка
    услуги с valid_from <= t (индекс (service_id, valid_from), LIMIT 1), как LATERAL в Y.
    Корректно, потому что цепочка упорядочена по valid_from, а непривязанные
    запланированные строки лежат в будущем (valid_from > t)."""
    name = "X·lat"
    only_scenarios = {"7"}

    def version_page(self, branch, version_id, cursor, limit, type_ids):
        op = super().version_page(branch, version_id, cursor, limit, type_ids)
        old = """JOIN LATERAL (
  SELECT r.* FROM x_row r LEFT JOIN x_row c ON c.id = r.child_id
  WHERE r.service_id = p.id AND r.valid_from <= v.t AND (c.id IS NULL OR c.valid_from > v.t)
) r ON true"""
        new = """JOIN LATERAL (
  SELECT r.* FROM x_row r WHERE r.service_id = p.id AND r.valid_from <= v.t
  ORDER BY r.valid_from DESC LIMIT 1
) r ON true"""
        assert old in op.steps[0].sql, "формула среза X изменилась — обнови XModelLat"
        op.steps[0].sql = op.steps[0].sql.replace(old, new)
        return op
