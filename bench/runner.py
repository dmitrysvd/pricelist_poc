"""Раннер: выполняет Op моделей и снимает метрики одинаково для X и Y."""
import json
import statistics
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import psycopg
from django.conf import settings

from bench.iface import Op, Ref

MODEL_TABLES = {"X": ["x_row", "x_version"], "X(б)": ["x_row", "x_version"], "X·lat": ["x_row", "x_version"],
                "Y": ["y_value", "y_version"],
                "общая": ["service", "category", "price_type"]}
RESULTS = Path(settings.BASE_DIR) / "results"
LONG_MS, LONG_RUNS = 10_000, 3


# Параметры сессии для всех соединений стенда (например {"work_mem": "512MB"}).
SESSION_SETTINGS: dict = {}


def connect(dbname, autocommit=False):
    conn = psycopg.connect(host=settings.PG_SOCKET, user="bench", password="bench", dbname=dbname,
                           autocommit=autocommit, prepare_threshold=None)
    for k, v in SESSION_SETTINGS.items():
        conn.execute(f"SET {k} = '{v}'")
        if not autocommit:
            conn.commit()
    return conn


def resolve(params, results):
    return {k: (results[v.step][v.col] if isinstance(v, Ref) else v) for k, v in params.items()}


def run_steps(cur, op: Op, explain_prefix: str | None = None):
    """Выполнить шаги Op; если explain_prefix задан — шаг op.explain под EXPLAIN."""
    results, rows_out, plan = {}, 0, None
    for i, st in enumerate(op.steps):
        p = resolve(st.params, results)
        if explain_prefix is not None and i == op.explain:
            cur.execute(explain_prefix + st.sql, p)
            plan = "\n".join(r[0] for r in cur.fetchall())
            # EXPLAIN ANALYZE выполняет шаг, но его строки не попадают в results:
            # op.explain должен указывать на шаг, на который не ссылается ни один Ref.
            continue
        cur.execute(st.sql, p)
        if cur.description:
            rows = cur.fetchall()
            rows_out = len(rows)
            if rows:
                results[st.name] = dict(zip([d.name for d in cur.description], rows[0]))
        else:
            rows_out = cur.rowcount
    return rows_out, plan


@dataclass
class Result:
    dataset: str
    scenario: str
    variant: str
    model: str
    times_ms: list = field(default_factory=list)
    # время отложенных проверок FK (SET CONSTRAINTS ALL IMMEDIATE), входит в times_ms
    fk_ms: list = field(default_factory=list)
    rows_returned: int | None = None
    wal_bytes: list = field(default_factory=list)
    tuples: dict | None = None
    size_delta: dict | None = None
    explain_file: str | None = None
    note: str = ""
    error: str | None = None

    @property
    def p50(self):
        return statistics.median(self.times_ms) if self.times_ms else None

    @property
    def p95(self):
        if not self.times_ms:
            return None
        s = sorted(self.times_ms)
        return s[min(len(s) - 1, round(0.95 * (len(s) - 1)))]

    def to_dict(self):
        d = asdict(self)
        d["p50"], d["p95"] = self.p50, self.p95
        return d


def save_plan(dataset, scenario, variant, model, plan):
    d = RESULTS / "explain" / dataset
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{scenario}_{variant}_{model}.txt".replace(" ", "_").replace("/", "-")
    f.write_text(plan)
    return str(f.relative_to(RESULTS))


def model_sizes(cur, model):
    cur.execute("SELECT relname, pg_relation_size(oid), pg_indexes_size(oid), pg_total_relation_size(oid) "
                "FROM pg_class WHERE relname = ANY(%s)", (MODEL_TABLES[model],))
    return {r[0]: {"heap": r[1], "indexes": r[2], "total": r[3]} for r in cur.fetchall()}


def xact_tuples(cur, model):
    cur.execute("SELECT coalesce(sum(n_tup_ins),0), coalesce(sum(n_tup_upd),0), coalesce(sum(n_tup_del),0) "
                "FROM pg_stat_xact_user_tables WHERE relname = ANY(%s)", (MODEL_TABLES[model],))
    i, u, d = cur.fetchone()
    return {"ins": int(i), "upd": int(u), "del": int(d)}


def run_read(conn, dataset, scenario, variant, model, ops: list[Op], warmup=3):
    """ops — по одной Op на прогон (разные параметры, одинаковые для X и Y)."""
    res = Result(dataset, scenario, variant, model.name)
    try:
        with conn.cursor() as cur:
            for op in ops[:warmup]:
                run_steps(cur, op)
            conn.rollback()
            for op in ops:
                t0 = time.perf_counter()
                res.rows_returned, _ = run_steps(cur, op)
                res.times_ms.append((time.perf_counter() - t0) * 1000)
                conn.rollback()
            _, plan = run_steps(cur, ops[0], "EXPLAIN (ANALYZE, BUFFERS, SETTINGS) ")
            conn.rollback()
            res.explain_file = save_plan(dataset, scenario, variant, model.name, plan)
    except Exception as e:  # noqa: BLE001 — фиксируем в отчёте, прогон идёт дальше
        conn.rollback()
        res.error = f"{type(e).__name__}: {e}"
    return res


def run_write(conn, dataset, scenario, variant, model, make_op, runs=5, vacuum=True):
    """make_op(i) -> Op. Каждый прогон в своей транзакции с ROLLBACK."""
    res = Result(dataset, scenario, variant, model.name)
    try:
        with conn.cursor() as cur:
            i = 0
            while i < runs:
                op = make_op(i)
                cur.execute("SELECT pg_current_wal_insert_lsn()")
                lsn0 = cur.fetchone()[0]
                size0 = model_sizes(cur, model.name) if i == 0 else None
                t0 = time.perf_counter()
                rows, _ = run_steps(cur, op)
                t_fk = time.perf_counter()
                cur.execute("SET CONSTRAINTS ALL IMMEDIATE")
                t1 = time.perf_counter()
                res.times_ms.append((t1 - t0) * 1000)
                res.fk_ms.append((t1 - t_fk) * 1000)
                cur.execute("SELECT pg_wal_lsn_diff(pg_current_wal_insert_lsn(), %s)", (lsn0,))
                res.wal_bytes.append(int(cur.fetchone()[0]))
                if i == 0:
                    res.tuples = xact_tuples(cur, model.name)
                    size1 = model_sizes(cur, model.name)
                    res.size_delta = {t: {k: size1[t][k] - size0[t][k] for k in size1[t]} for t in size1}
                    res.rows_returned = rows
                conn.rollback()
                if vacuum:
                    conn.autocommit = True
                    cur.execute(f"VACUUM {', '.join(MODEL_TABLES[model.name])}")
                    conn.autocommit = False
                # Операции дольше LONG_MS: шум между прогонами — доли процента,
                # медиане хватает 3 прогонов (p95 тогда = максимум, отчёт это помечает).
                if i == 0 and res.times_ms[0] > LONG_MS:
                    runs = min(runs, LONG_RUNS)
                i += 1
            # EXPLAIN (ANALYZE, BUFFERS, WAL) ключевого шага — отдельная транзакция с откатом
            op = make_op(0)
            _, plan = run_steps(cur, op, "EXPLAIN (ANALYZE, BUFFERS, WAL, SETTINGS) ")
            conn.rollback()
            res.explain_file = save_plan(dataset, scenario, variant, model.name, plan)
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        conn.autocommit = False
        res.error = f"{type(e).__name__}: {e}"
    return res


def dump(results: list[Result], dataset, tag):
    d = RESULTS / "raw"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{dataset}_{tag}.json"
    f.write_text(json.dumps([r.to_dict() for r in results], ensure_ascii=False, indent=1, default=str))
    return f
