"""Сверка: X и Y на одних параметрах возвращают одно и то же (по смыслу).

uv run python manage.py verify --datasets t1,t5,t20
"""
from decimal import Decimal

from django.core.management.base import BaseCommand

from bench.runner import connect, run_steps
from bench.scenarios import load_ctx, read_scenarios
from model_x.impl import XModel
from model_y.impl import YModel


def D(v):
    return None if v is None else Decimal(str(v)).quantize(Decimal("0.01"))


def x_prices(price, overrides, main):
    d = {main: D(price)}
    d.update({int(k): D(v) for k, v in (overrides or {}).items()})
    return d


def norm(sc, model, rows, cols, ctx):
    r = [dict(zip(cols, row)) for row in rows]
    if sc == "1":
        return sorted((x["id"], D(x["main_price"]), D(x["ins_price"])) for x in r)
    if sc == "2":
        out = []
        for x in r:
            if model == "Y":
                prices = {int(k): D(v) for k, v in (x["prices"] or {}).items()}
                sched = {int(k): D(v["value"]) for k, v in (x["scheduled"] or {}).items()}
            else:
                prices = x_prices(x["price"], x["price_overrides"], ctx.main)
                sched = {}
                if x["sch_at"] is not None:
                    s = x_prices(x["sch_price"], x["sch_overrides"], ctx.main)
                    sched = {t: s.get(t) for t in set(s) | set(prices) if s.get(t) != prices.get(t)}
            out.append((x["id"], prices, sched))
        return out
    if sc == "7":
        out = {}
        if model == "Y":
            for x in r:
                if x["value"] is not None or x["changed"]:
                    out[(x["id"], x["price_type_id"])] = (D(x["value"]), bool(x["changed"]),
                                                           D(x["old_value"]) if x["changed"] else None)
        else:
            for x in r:
                cur = x_prices(x["price"], x["price_overrides"], ctx.main)
                old = x_prices(x["old_price"], x["old_overrides"], ctx.main) if x["old_price"] is not None else {}
                for t in ctx.types:
                    ch = bool(x["changed"]) and (cur.get(t) != old.get(t))
                    if cur.get(t) is not None or ch:
                        out[(x["service_id"], t)] = (cur.get(t), ch, old.get(t) if ch else None)
        return out
    if sc == "8":
        if "value" not in cols:  # список версий
            return [x["id"] for x in r]
        return [(D(x["value"]), x["created_at"]) for x in r]
    return r


class Command(BaseCommand):
    def add_arguments(self, p):
        p.add_argument("--datasets", default="t5")
        p.add_argument("--n", type=int, default=5)

    def handle(self, *a, **o):
        bad = 0
        for ds in o["datasets"].split(","):
            conn = connect(f"bench_{ds}")
            with conn.cursor() as cur:
                ctx = load_ctx(cur)
                from model_x.impl_b import XModelB, XModelLat
                from model_y2.impl import Y2Model
                y = YModel()
                from model_y3.impl import Y3Model
                xs = [XModel(), XModelB(), XModelLat(), Y2Model()]
                cur.execute("SELECT to_regclass('y3_current') IS NOT NULL AND EXISTS (SELECT 1 FROM y3_current)")
                from model_y.impl_sq import Y3ModelSq, YModelSq
                xs.append(YModelSq())
                if cur.fetchone()[0]:
                    xs += [Y3Model(), Y3ModelSq()]
                for xm in xs:
                    xm.main_type = ctx.main
                for (sc, variant, make), x in ((t, xm) for t in read_scenarios(ctx, o["n"]) for xm in xs):
                    if getattr(x, "only_scenarios", None) and sc not in x.only_scenarios:
                        continue
                    if x.name not in ("X", "Y2", "Y3") and sc not in getattr(x, "only_scenarios", ()):
                        continue
                    variant = f"{variant} [{x.name}]"
                    diffs = 0
                    for ox, oy in list(zip(make(x), make(y)))[:o["n"]]:
                        res = {}
                        # Y2 возвращает данные в форме Y, X-варианты — в форме X
                        for name, shape, op in (("X", "Y" if x.name.startswith("Y") else "X", ox), ("Y", "Y", oy)):
                            for st in op.steps:
                                cur.execute(st.sql, st.params)
                            res[name] = norm(sc, shape, cur.fetchall(), [d.name for d in cur.description], ctx)
                        if res["X"] != res["Y"]:
                            diffs += 1
                            if diffs == 1:
                                self.stdout.write(f"    X: {str(res['X'])[:400]}\n    Y: {str(res['Y'])[:400]}")
                    conn.rollback()
                    bad += diffs
                    mark = self.style.SUCCESS("ok") if not diffs else self.style.ERROR(f"РАЗНИЦА в {diffs}")
                    n_sched = sum(len(e[2]) for e in res["Y"]) if sc == "2" else ""
                    self.stdout.write(f"[{ds}] сц.{sc} {variant:<38} {mark}  размер={len(res['X'])} {n_sched}")
            conn.close()
        if bad:
            raise SystemExit(1)
