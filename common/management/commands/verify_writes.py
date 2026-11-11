"""Сверка записей: после каждой операции (с COMMIT на клоне) актуальные цены X == Y.

uv run python manage.py verify_writes --datasets t5 --scenarios 3,4,5,6,9,10,11,12
"""
from django.core.management.base import BaseCommand

from bench.runner import connect, run_steps
from bench.scenarios import clone_setup, load_ctx, write_scenarios
from model_x.impl import XModel
from model_y.impl import YModel

# Актуальные цены всех пар: Y — status 'A'; X — хвост цепочки с valid_from <= горизонт
# (горизонт > now(), чтобы применённая заранее запланированная версия была видна в X:
# в продукте применение происходит в момент наступления даты).
DIFF = """
WITH y AS (SELECT service_id, price_type_id, value FROM y_value WHERE status = 'A' AND value IS NOT NULL),
x AS (
  SELECT r.service_id, pt.id AS price_type_id,
         CASE WHEN pt.kind = 'main' THEN r.price ELSE (r.price_overrides ->> pt.id::text)::numeric END AS value
  FROM x_row r JOIN service s ON s.id = r.service_id JOIN price_type pt ON pt.branch_id = s.branch_id
  WHERE r.child_id IS NULL AND r.valid_from <= now() + interval '8 days'
    AND r.id NOT IN (SELECT unnest(row_ids) FROM x_version WHERE status = 'S')
), xn AS (SELECT * FROM x WHERE value IS NOT NULL)
SELECT (SELECT count(*) FROM (SELECT * FROM y EXCEPT SELECT * FROM xn) a),
       (SELECT count(*) FROM (SELECT * FROM xn EXCEPT SELECT * FROM y) b),
       (SELECT count(*) FROM y)
"""

# Восстановление: актуальные цены филиала == срез Y на версию V.
RESTORE_CHECK = """
WITH n AS (SELECT number AS n FROM y_version WHERE id = %(v)s),
old AS (
  SELECT DISTINCT ON (v.service_id, v.price_type_id) v.service_id, v.price_type_id, v.value
  FROM y_value v JOIN service s ON s.id = v.service_id, n
  WHERE s.branch_id = 1 AND v.ver <= n.n ORDER BY v.service_id, v.price_type_id, v.ver DESC
), cur AS (
  SELECT a.service_id, a.price_type_id, a.value FROM y_value a JOIN service s ON s.id = a.service_id
  WHERE s.branch_id = 1 AND a.status = 'A'
)
SELECT count(*) FROM ((SELECT * FROM old WHERE value IS NOT NULL EXCEPT SELECT * FROM cur WHERE value IS NOT NULL)
  UNION ALL (SELECT * FROM cur WHERE value IS NOT NULL EXCEPT SELECT * FROM old WHERE value IS NOT NULL)) d
"""

# Применение к филиалу: цены B == цены A по сопоставлению услуг и типов.
COPY_CHECK = """
WITH tm AS (SELECT a.id a_t, b.id b_t FROM price_type a JOIN price_type b ON b.branch_id = 2 AND b.sort = a.sort
            WHERE a.branch_id = 1),
av AS (SELECT m.b_id, tm.b_t, v.value FROM y_value v JOIN service_map m ON m.a_id = v.service_id
       JOIN tm ON tm.a_t = v.price_type_id WHERE v.status = 'A' AND v.value IS NOT NULL),
bv AS (SELECT v.service_id, v.price_type_id, v.value FROM y_value v JOIN service s ON s.id = v.service_id
       WHERE s.branch_id = 2 AND v.status = 'A' AND v.value IS NOT NULL)
SELECT count(*) FROM ((SELECT * FROM av EXCEPT SELECT * FROM bv) UNION ALL (SELECT * FROM bv EXCEPT SELECT * FROM av)) d
"""


class Command(BaseCommand):
    def add_arguments(self, p):
        p.add_argument("--datasets", default="t5")
        p.add_argument("--scenarios", default="3,4,5,6,9,10,11,12")

    def handle(self, *a, **o):
        only = set(o["scenarios"].split(","))
        bad = 0
        for ds in o["datasets"].split(","):
            base = f"bench_{ds}"
            c = connect(base)
            with c.cursor() as cur:
                ctx = load_ctx(cur)
            c.close()
            for sc, variant, setup, make, _ in write_scenarios(ctx):
                if sc not in only:
                    continue
                clone = f"{base}_v{sc}"
                admin = connect("bench", autocommit=True)
                admin.execute(f"DROP DATABASE IF EXISTS {clone}")
                admin.execute(f"CREATE DATABASE {clone} TEMPLATE {base} STRATEGY FILE_COPY")
                conn = connect(clone)
                try:
                    csql, cparams, _ = clone_setup(sc, ctx)
                    with conn.cursor() as cur:
                        if csql:
                            for stmt in filter(str.strip, csql.split(";")):
                                cur.execute(stmt, cparams)
                        x, y = XModel(), YModel()
                        x.main_type = ctx.main
                        for m in (x, y):
                            arg = None
                            if setup:
                                run_steps(cur, setup(m))
                                t = "x_version" if m.name == "X" else "y_version"
                                cur.execute(f"SELECT max(id) FROM {t} WHERE status = 'S'")
                                arg = cur.fetchone()[0]
                            if sc == "10" and m.name == "Y":
                                # тип цены общий: Y заполняет тот же тип, что создал шаг X
                                cur.execute("SELECT max(id) FROM price_type")
                                tid = cur.fetchone()[0]
                                op = make(m, arg)(0)
                                op.steps[0].sql = "SELECT %(tid)s::bigint AS id, %(tid)s::text AS key"
                                op.steps[0].params = {"tid": tid}
                                run_steps(cur, op)
                            elif sc == "12" and m.name == "Y":
                                # общая часть импорта уже выполнена шагами X: Y берёт те же услуги
                                op = m.import_prices(3, 1)
                                op.steps = [s for s in op.steps if s.name not in ("cats", "svcs")]
                                run_steps(cur, op)
                            else:
                                run_steps(cur, make(m, arg)(0))
                        cur.execute("SET CONSTRAINTS ALL IMMEDIATE")
                        cur.execute(DIFF)
                        only_y, only_x, total = cur.fetchone()
                        extra = ""
                        if sc == "9":
                            cur.execute(RESTORE_CHECK, {"v": ctx.v_half})
                            extra = f", != срезу на V: {cur.fetchone()[0]}"
                        if sc == "11":
                            cur.execute(COPY_CHECK)
                            extra = f", B != A: {cur.fetchone()[0]}"
                    conn.rollback()
                    ok = only_x == only_y == 0 and (not extra or extra.endswith(": 0"))
                    bad += not ok
                    mark = self.style.SUCCESS("ok") if ok else self.style.ERROR("РАЗНИЦА")
                    self.stdout.write(f"[{ds}] сц.{sc} {variant:<48} {mark} (только Y: {only_y}, "
                                      f"только X: {only_x}, пар: {total}{extra})")
                finally:
                    conn.close()
                    admin.execute(f"DROP DATABASE IF EXISTS {clone}")
                    admin.close()
        if bad:
            raise SystemExit(1)
