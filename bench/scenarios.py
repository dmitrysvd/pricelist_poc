"""Сценарии ТЗ: параметры и прогоны, одинаковые для X и Y.

Контекст (типы, услуги, версии, курсоры) берётся из датасета один раз и
общий для обеих моделей; случайные параметры прогонов — из одного seed.
"""
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from bench.iface import Edit
from bench.runner import run_read, run_write

TZ = ZoneInfo("Europe/Moscow")
BRANCH = 1
AUTHOR = 1  # у автора 1 нет версии «сегодня»: правки открывают новую дневную версию


@dataclass
class Ctx:
    types: list[int]
    main: int
    ins: int | None
    services: list[int]
    cursors: list[tuple]
    v_half: int  # дневная версия ~полгода назад
    v_half_mass: int  # массовая (ежемесячная) версия ~полгода назад
    next_pairs: list[tuple]  # (service, type) с изменениями других типов до следующего изменения
    now: datetime
    hist_author: int = 7  # самый активный сотрудник в окне фильтра истории (не пустая выдача)


def load_ctx(cur):
    cur.execute("SELECT id FROM price_type WHERE branch_id = %s ORDER BY sort", (BRANCH,))
    types = [r[0] for r in cur.fetchall()]
    cur.execute("SELECT id FROM service WHERE branch_id = %s ORDER BY id", (BRANCH,))
    services = [r[0] for r in cur.fetchall()]
    cur.execute("""SELECT category_sort, code, name, id FROM (
        SELECT *, row_number() OVER (ORDER BY category_sort, code, name, id) rn
        FROM service WHERE branch_id = %s AND NOT archived) s
        WHERE rn %% 4000 = 0 ORDER BY rn""", (BRANCH,))
    cursors = [tuple(r) for r in cur.fetchall()]
    # дневная версия ~полгода назад с заметным числом изменений
    cur.execute("""SELECT v.id FROM y_version v
        WHERE v.branch_id = %s AND v.status = 'O' AND v.planned_at IS NULL
          AND v.created_at BETWEEN now() - interval '190 days' AND now() - interval '175 days'
          AND (SELECT count(*) FROM y_value x WHERE x.version_id = v.id) BETWEEN 60 AND 300
        ORDER BY abs(extract(epoch FROM v.created_at - (now() - interval '182 days'))) LIMIT 1""", (BRANCH,))
    v_half = cur.fetchone()[0]
    # массовая версия основного типа (85 тыс. изменений во всех датасетах — иначе
    # при ротации типов в t20 попадается версия страхового типа на ~51 тыс.)
    cur.execute("""SELECT v.id FROM y_version v
        WHERE v.branch_id = %s AND v.status = 'O' AND v.planned_at IS NOT NULL
          AND EXISTS (SELECT 1 FROM y_value x WHERE x.version_id = v.id AND x.price_type_id = %s)
        ORDER BY abs(extract(epoch FROM v.created_at - (now() - interval '182 days'))) LIMIT 1""",
                (BRANCH, types[0]))
    row = cur.fetchone()
    if row is None:  # в профиле single основного типа среди ежемесячных может не быть
        cur.execute("""SELECT v.id FROM y_version v
            WHERE v.branch_id = %s AND v.status = 'O' AND v.planned_at IS NOT NULL
            ORDER BY abs(extract(epoch FROM v.created_at - (now() - interval '182 days'))) LIMIT 1""", (BRANCH,))
        row = cur.fetchone()
    v_half_mass = row[0]
    # Пары для «следующего изменения после V»: между V и следующим изменением этой
    # пары были изменения других типов той же услуги (при 1 типе — просто следующее).
    cur.execute("""
        WITH n AS (SELECT number AS n FROM y_version WHERE id = %(v)s),
        nx AS (
          SELECT DISTINCT ON (v.service_id, v.price_type_id) v.service_id, v.price_type_id, v.ver
          FROM y_value v, n WHERE v.ver > n.n AND v.service_id <= %(maxs)s AND v.price_type_id = ANY(%(types)s)
          ORDER BY v.service_id, v.price_type_id, v.ver
        )
        SELECT nx.service_id, nx.price_type_id,
          (SELECT count(*) FROM y_value o, n WHERE o.service_id = nx.service_id
             AND o.price_type_id <> nx.price_type_id AND o.ver > n.n AND o.ver < nx.ver) AS others
        FROM nx WHERE nx.service_id %% 97 = 0
        ORDER BY others DESC, nx.service_id LIMIT 20""",
                {"v": v_half, "maxs": max(services), "types": types})
    next_pairs = [(r[0], r[1]) for r in cur.fetchall()]
    cur.execute("SELECT now()")
    now = cur.fetchone()[0]
    cur.execute("""SELECT author_id FROM y_version WHERE branch_id = %s
        AND created_at BETWEEN now() - interval '212 days' AND now() - interval '182 days'
        GROUP BY 1 ORDER BY count(*) DESC, 1 LIMIT 1""", (BRANCH,))
    return Ctx(types, types[0], types[1] if len(types) > 1 else None, services, cursors,
               v_half, v_half_mass, next_pairs, now, cur.fetchone()[0])


# --------------------------------------------------------------------- чтение
def read_scenarios(ctx: Ctx, runs: int):
    """[(scenario, variant, fn(model) -> [Op])] — op на каждый прогон."""
    out = []
    rnd = lambda tag: random.Random(f"{tag}")  # noqa: E731 — одинаковые параметры для X и Y

    for n in (1, 10, 50):
        def ops(m, n=n):
            r = rnd(f"s1-{n}")
            return [m.current_prices(r.sample(ctx.services, n), ctx.main, ctx.ins) for _ in range(runs + 3)]
        out.append(("1", f"{n} услуг", ops))

    def page_first(m):
        return [m.price_page(BRANCH, None, 50, ctx.types) for _ in range(runs + 3)]

    def page_cursor(m):
        r = rnd("s2c")
        return [m.price_page(BRANCH, r.choice(ctx.cursors), 50, ctx.types) for _ in range(runs + 3)]

    out += [("2", "первая страница", page_first), ("2", "курсор в середине", page_cursor)]
    if ctx.ins:
        def page_missing(m):
            r = rnd("s2m")
            return [m.price_page(BRANCH, r.choice(ctx.cursors), 50, ctx.types, missing_type=ctx.ins)
                    for _ in range(runs + 3)]
        out.append(("2", "только без цены (страховая)", page_missing))

    for tag, vid in (("дневная версия", ctx.v_half), ("массовая версия", ctx.v_half_mass)):
        def vpage(m, vid=vid, tag=tag):
            r = rnd(f"s7{tag}")
            return [m.version_page(BRANCH, vid, r.choice(ctx.cursors), 50, ctx.types) for _ in range(runs + 3)]
        out.append(("7", f"{tag}, ~полгода назад", vpage))

    half = ctx.now - timedelta(days=182)
    hist = [
        ("без фильтров", dict(date_from=None, date_to=None, author=None, price_type=None)),
        ("сотрудник", dict(date_from=None, date_to=None, author=ctx.hist_author, price_type=None)),
        ("тип цены", dict(date_from=None, date_to=None, author=None, price_type=ctx.ins or ctx.main)),
        ("месяц + сотрудник + тип", dict(date_from=half - timedelta(days=30), date_to=half, author=ctx.hist_author,
                                         price_type=ctx.ins or ctx.main)),
    ]
    for tag, kw in hist:
        def h(m, kw=kw):
            return [m.history(BRANCH, limit=50, **kw) for _ in range(runs + 3)]
        out.append(("8", f"история: {tag}", h))

    def nxt(m):
        pairs = (ctx.next_pairs * ((runs + 3) // max(1, len(ctx.next_pairs)) + 1))[:runs + 3]
        return [m.next_change(s, t, ctx.v_half, t == ctx.main) for s, t in pairs]
    out.append(("8", "следующее изменение пары после V", nxt))
    return out


def run_reads(conn, dataset, models, ctx, runs, only):
    results = []
    for sc, variant, make in read_scenarios(ctx, runs):
        if sc not in only:
            continue
        for m in models:
            if getattr(m, "only_scenarios", None) and sc not in m.only_scenarios:
                continue
            ops = make(m)
            r = run_read(conn, dataset, sc, variant, m, ops)
            results.append(r)
            print(f"  [{dataset}] сц.{sc} {variant:<38} {m.name}: p50={r.p50 and round(r.p50, 2)} ms"
                  f"{' ERR ' + r.error if r.error else ''}", flush=True)
    return results


# --------------------------------------------------------------------- запись
def write_scenarios(ctx: Ctx):
    """[(scenario, variant, setup(model)->Op|None, make(model, setup_result)->fn(i)->Op, runs)]"""
    planned = ctx.now + timedelta(days=7)
    out = [
        ("3", "а) сразу, основная +10%", None,
         lambda m, _: (lambda i: m.mass_change(BRANCH, AUTHOR, ctx.main, True, 10, None)), 5),
        ("3", "б) запланированно, основная +10%", None,
         lambda m, _: (lambda i: m.mass_change(BRANCH, AUTHOR, ctx.main, True, 10, planned)), 5),
    ]
    sched = lambda m: m.mass_change(BRANCH, AUTHOR, ctx.main, True, 10, planned)  # noqa: E731
    out += [
        ("4", "применение запланированной (основная, весь прайс)", sched,
         lambda m, vid: (lambda i: m.apply_scheduled(BRANCH, vid)), 5),
        ("5", "отмена запланированной (основная, весь прайс)", sched,
         lambda m, vid: (lambda i: m.cancel_scheduled(BRANCH, vid)), 5),
    ]

    def edits(m, i, k):
        r = random.Random(f"s6-{i}")
        res = []
        for _ in range(k):
            t = r.choice(ctx.types)
            res.append(Edit(r.choice(ctx.services), t, f"{r.randint(500, 50000)}.00"))
        return res

    def chain(m, i, k):
        from bench.iface import Op
        steps = []
        for j, e in enumerate(edits(m, i, k)):
            op = m.point_edit(BRANCH, AUTHOR, e, ctx.main)
            for s in op.steps:
                s.name = f"{s.name}{j}"
                s.params = {kk: (type(v)(f"{v.step}{j}", v.col) if hasattr(v, "step") else v)
                            for kk, v in s.params.items()}
            steps += op.steps
        return Op(steps, explain=len(steps) - 1)

    out += [
        ("6", "1 правка (открывает дневную версию)", None, lambda m, _: (lambda i: chain(m, i, 1)), 20),
        ("6", "10 правок разных цен в одну дневную версию", None, lambda m, _: (lambda i: chain(m, i, 10)), 20),
        ("9", "восстановление версии ~полгода назад, все типы", None,
         lambda m, _: (lambda i: m.restore(BRANCH, AUTHOR, ctx.v_half, ctx.types)), 5),
        ("10", "новый тип = основная −10%", None,
         lambda m, _: (lambda i: m.add_price_type(BRANCH, AUTHOR, ctx.main, -10)), 5),
        ("11", "прайс филиала 1 -> филиал 2 (те же услуги)", None,
         lambda m, _: (lambda i: m.copy_branch(BRANCH, 2, AUTHOR)), 5),
        ("12", "импорт 85 тыс. услуг в пустой прайс", None,
         lambda m, _: (lambda i: m.import_prices(3, AUTHOR)), 5),
    ]
    return out


def clone_setup(sc, ctx):
    """SQL подготовки клона (коммитится, не замеряется) и нужна ли строка «общая»."""
    from bench.common_ops import IMPORT_STAGING_SETUP, SERVICE_MAP_SETUP
    if sc == "11":
        return SERVICE_MAP_SETUP, {"n": len(ctx.services), "src": BRANCH, "dst": 2}, False
    if sc == "12":
        return IMPORT_STAGING_SETUP, {"n": len(ctx.services)}, True
    return None, None, False


def run_writes(base_db, dataset, models, ctx, only, connect, write_runs=None):
    results = []
    for sc, variant, setup, make, runs in write_scenarios(ctx):
        if sc not in only:
            continue
        runs = write_runs or runs
        clone = f"{base_db}_w{sc}"
        admin = connect("bench", autocommit=True)
        admin.execute(f"DROP DATABASE IF EXISTS {clone}")
        admin.execute(f"CREATE DATABASE {clone} TEMPLATE {base_db} STRATEGY FILE_COPY")
        conn = connect(clone)
        try:
            csql, cparams, with_common = clone_setup(sc, ctx)
            if csql:
                with conn.cursor() as cur:
                    for stmt in filter(str.strip, csql.split(";")):
                        cur.execute(stmt, cparams)
                conn.commit()
            run_models = list(models)
            if with_common:
                from bench.common_ops import CommonOnly
                run_models = [CommonOnly()] + run_models
            for m in run_models:
                if getattr(m, "only_scenarios", None) and sc not in m.only_scenarios:
                    continue
                setup_res = None
                if setup:
                    from bench.runner import run_steps
                    with conn.cursor() as cur:
                        op = setup(m)
                        run_steps(cur, op)
                        # id созданной запланированной версии — из шага "ver"
                        cur.execute(f"SELECT max(id) FROM {'x_version' if m.name.startswith('X') else 'y_version'} "
                                    "WHERE status = 'S'")
                        setup_res = cur.fetchone()[0]
                    conn.commit()
                    conn.autocommit = True
                    # только таблицы этой модели: общие таблицы подготовка не меняла,
                    # а таблицы другой модели к замеру не относятся
                    from bench.runner import MODEL_TABLES
                    conn.execute(f"VACUUM ANALYZE {', '.join(MODEL_TABLES[m.name])}")
                    conn.execute("CHECKPOINT")
                    conn.autocommit = False
                r = run_write(conn, dataset, sc, variant, m, make(m, setup_res), runs=runs)
                results.append(r)
                print(f"  [{dataset}] сц.{sc} {variant:<45} {m.name}: p50={r.p50 and round(r.p50, 1)} ms "
                      f"wal={r.wal_bytes and r.wal_bytes[0] // 1024}kB tup={r.tuples}"
                      f"{' ERR ' + r.error if r.error else ''}", flush=True)
        finally:
            conn.close()
            admin.execute(f"DROP DATABASE IF EXISTS {clone}")
            admin.close()
    return results
