"""Прогон сценариев на датасетах: uv run python manage.py bench --datasets t1,t5,t20 --scenarios 1,2,3,7,8"""
from django.core.management.base import BaseCommand

from bench.runner import connect, dump
from bench.scenarios import load_ctx, run_reads, run_writes
from model_x.impl import XModel
from model_y.impl import YModel

READ = {"1", "2", "7", "8"}


class Command(BaseCommand):
    def add_arguments(self, p):
        p.add_argument("--datasets", default="t1,t5,t20")
        p.add_argument("--scenarios", default="1,2,3,4,5,6,7,8")
        p.add_argument("--runs", type=int, default=20)
        p.add_argument("--write-runs", type=int, default=None, help="переопределить число прогонов записи")
        p.add_argument("--tag", default="")
        p.add_argument("--variants", action="store_true", help="добавить X(б) (сц.1,4) и X·lat (сц.7)")
        p.add_argument("--work-mem", default=None, help="work_mem сессии, например 512MB")
        p.add_argument("--models", default="X,Y", help="какие модели мерить: X,Y,Y2")

    def handle(self, *a, **o):
        only = set(o["scenarios"].split(","))
        if o["work_mem"]:
            from bench import runner
            runner.SESSION_SETTINGS["work_mem"] = o["work_mem"]
        for ds in o["datasets"].split(","):
            db = f"bench_{ds}"
            conn = connect(db)
            with conn.cursor() as cur:
                ctx = load_ctx(cur)
            conn.rollback()
            from model_y2.impl import Y2Model
            x, y = XModel(), YModel()
            x.main_type = ctx.main
            from model_y3.impl import Y3Model
            from model_y.impl_sq import Y3ModelSq, YModelSq
            by_name = {"X": x, "Y": y, "Y2": Y2Model(), "Y3": Y3Model(), "Y·кв": YModelSq(), "Y3·кв": Y3ModelSq()}
            models = [by_name[n] for n in o["models"].split(",")]
            if o["variants"]:
                from model_x.impl_b import XModelB, XModelLat
                xb, xl = XModelB(), XModelLat()
                xb.main_type = xl.main_type = ctx.main
                models += [xb, xl]
            self.stdout.write(f"== {ds}: типов {len(ctx.types)}, V(полгода)={ctx.v_half}, "
                              f"V(массовая)={ctx.v_half_mass}, пар для сц.8б: {len(ctx.next_pairs)}")
            res = []
            if only & READ:
                from bench.runner import prewarm
                n, pages = prewarm(conn)
                self.stdout.write(f"   prewarm: {n} отношений, {pages * 8 // 1024} МБ")
                res += run_reads(conn, ds, models, ctx, o["runs"], only)
            conn.close()
            if only - READ:
                res += run_writes(db, ds, models, ctx, only - READ, connect, o["write_runs"])
            f = dump(res, ds, o["tag"] or "_".join(sorted(only)))
            self.stdout.write(self.style.SUCCESS(f"   -> {f}"))
