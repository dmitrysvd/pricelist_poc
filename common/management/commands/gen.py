"""Генерация датасета: общие таблицы + год истории цен, одинаковой для X и Y.

Источник истины — каноническая лента версий: каждая версия = набор
(услуга, тип цены, новое значение в копейках | -1 = цена очищена).
Из ленты строятся обе модели:
  Y — одна строка значения на изменённую пару услуга x тип;
  X — одна строка на (версия, услуга) с полным снимком всех цен услуги.

Филиалы:
  1 — основной: S услуг, год истории, одна висящая запланированная версия;
  2 — получатель для сц. 11 (применение прайса к филиалу): S услуг, только импорт;
  3 — пустой прайс для сц. 12 (импорт xlsx): только типы цен.
"""
import io
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
from django.core.management.base import BaseCommand
from django.db import connections

TZ = ZoneInfo("Europe/Moscow")
EMPTY = -1


def type_kinds(n):
    """Набор типов цен филиала: 1 -> только основная; 5 -> по одному каждого вида."""
    if n == 1:
        return ["main"]
    if n == 5:
        return ["main", "ins", "contr", "zp", "custom"]
    rest = n - 1
    ins = rest * 8 // 19
    contr = rest * 8 // 19
    custom = rest - ins - contr - 1
    return ["main"] + ["ins"] * ins + ["contr"] * contr + ["zp"] + ["custom"] * custom


@dataclass
class Version:
    branch: int
    author: int
    created_at: float  # epoch
    applied_at: float | None  # None = висит запланированной
    planned_at: float | None = None
    svc: np.ndarray = None  # индексы услуг (0..S-1)
    typ: np.ndarray = None  # индексы типов (0..T-1), 0 = основная
    val: np.ndarray = None  # копейки, EMPTY = очищена
    ts: np.ndarray = None  # момент правки каждой пары (epoch)
    id: int = 0
    number: int | None = None
    x_row_ids: list = field(default_factory=list)


def dedupe_last(svc, typ, val, ts, S):
    """В одной версии пара меняется максимум один раз: оставляем последнее изменение."""
    key = typ.astype(np.int64) * S + svc
    rev = key[::-1]
    _, idx = np.unique(rev, return_index=True)
    keep = len(key) - 1 - idx
    keep.sort()
    return svc[keep], typ[keep], val[keep], ts[keep]


def pct(cents, p):
    """+p% с округлением до копеек (half-up), пустые цены не трогаем."""
    out = np.where(cents >= 0, (cents * (100 + p) + 50) // 100, EMPTY)
    return out.astype(np.int64)


class Command(BaseCommand):
    help = "Сгенерировать датасет (общие таблицы + история X и Y)"

    def add_arguments(self, p):
        p.add_argument("--database", required=True)
        p.add_argument("--types", type=int, required=True)
        p.add_argument("--services", type=int, default=85_000)
        p.add_argument("--categories", type=int, default=1_000)
        p.add_argument("--days", type=int, default=365)
        p.add_argument("--fill", type=float, default=0.6, help="доля заполненных неосновных цен")
        p.add_argument("--seed", type=int, default=42)
        p.add_argument("--profile", choices=["orig", "single"], default="orig",
                       help="orig: ежемесячно по одному типу (по кругу) + ежеквартально по всем типам; "
                            "single: только ежемесячно по одному случайному типу")

    # ------------------------------------------------------------------ main
    def handle(self, *a, **o):
        t0 = time.time()
        self.rng = np.random.default_rng(o["seed"])
        self.S, self.T = o["services"], o["types"]
        self.profile = o["profile"]
        self.conn = connections[o["database"]]
        self.conn.ensure_connection()
        self.raw = self.conn.connection
        self.raw.autocommit = True
        today = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        self.today = today
        self.start = today - timedelta(days=o["days"])

        self.truncate()
        self.n_emp = 20
        self.copy("employee", ["id", "name"], ((i, f"Сотрудник {i}") for i in range(1, self.n_emp + 1)))
        self.copy("branch", ["id", "name"], ((b, f"Филиал {b}") for b in (1, 2, 3)))

        # типы цен: одинаковый набор у трёх филиалов, id = (branch-1)*T + idx + 1
        kinds = type_kinds(self.T)
        self.type_ids = {b: np.arange(self.T) + (b - 1) * self.T + 1 for b in (1, 2, 3)}
        self.copy(
            "price_type", ["id", "branch_id", "kind", "name", "sort"],
            ((int(self.type_ids[b][i]), b, k, f"{k} {i}", i) for b in (1, 2, 3) for i, k in enumerate(kinds)),
        )

        # категории и услуги филиалов 1 и 2
        C = o["categories"]
        self.svc_ids = {}
        for b in (1, 2):
            cat0 = (b - 1) * C
            self.copy("category", ["id", "branch_id", "name", "sort"],
                      ((cat0 + c + 1, b, f"Категория {c + 1:04d}", c + 1) for c in range(C)))
            cat = np.sort(self.rng.integers(0, C, self.S))
            svc0 = (b - 1) * self.S
            self.svc_ids[b] = np.arange(self.S, dtype=np.int64) + svc0 + 1
            self.copy(
                "service", ["id", "branch_id", "category_id", "category_sort", "code", "name", "archived"],
                ((svc0 + i + 1, b, cat0 + int(cat[i]) + 1, int(cat[i]) + 1, f"A{int(cat[i]) + 1:04d}.{i:06d}",
                  f"Услуга {i:06d} приём/исследование/процедура", "f") for i in range(self.S)),
            )
        self.stdout.write(f"общие таблицы: {time.time() - t0:.1f}s")

        # история
        self.versions: list[Version] = []
        self.history_branch1(o["days"], o["fill"])
        self.import_only(branch=2, fill=o["fill"])
        self.stdout.write(f"лента версий: {len(self.versions)} версий, {time.time() - t0:.1f}s")
        self.write_models()
        self.stdout.write(f"запись X/Y: {time.time() - t0:.1f}s")
        self.finish()
        self.stdout.write(self.style.SUCCESS(f"готово за {time.time() - t0:.1f}s"))

    # ------------------------------------------------------------ helpers
    def truncate(self):
        with self.raw.cursor() as c:
            c.execute("TRUNCATE x_version, x_row, y_value, y_version, service, category, price_type, "
                      "employee, branch RESTART IDENTITY CASCADE")

    def copy(self, table, cols, rows):
        with self.raw.cursor() as c:
            with c.copy(f"COPY {table} ({', '.join(cols)}) FROM STDIN") as cp:
                for r in rows:
                    cp.write_row(r)

    def copy_text(self, table, cols, buf: io.StringIO):
        buf.seek(0)
        with self.raw.cursor() as c:
            with c.copy(f"COPY {table} ({', '.join(cols)}) FROM STDIN") as cp:
                while chunk := buf.read(1 << 20):
                    cp.write(chunk)

    def day_ts(self, d, hour=0.0):
        return (self.start + timedelta(days=d, hours=hour)).timestamp()

    def author(self):
        return int(self.rng.integers(1, self.n_emp + 1))

    # ------------------------------------------------------------ history
    def initial_state(self, fill):
        S, T, rng = self.S, self.T, self.rng
        st = np.full((T, S), EMPTY, dtype=np.int64)
        st[0] = rng.integers(500, 50_000, S) * 100  # основная: целые рубли
        for t in range(1, T):
            mask = rng.random(S) < fill
            st[t] = np.where(mask, (st[0] * rng.uniform(0.8, 1.5, S) / 100).round().astype(np.int64) * 100, EMPTY)
        return st

    def full_version(self, branch, st, ts, author, types=None):
        """Версия со всеми заполненными парами указанных типов (импорт / массовое)."""
        svc, typ, val = [], [], []
        for t in (range(self.T) if types is None else types):
            idx = np.nonzero(st[t] >= 0)[0]
            svc.append(idx)
            typ.append(np.full(len(idx), t))
            val.append(st[t, idx])
        svc, typ, val = np.concatenate(svc), np.concatenate(typ), np.concatenate(val)
        return Version(branch, author, ts, ts, svc=svc, typ=typ, val=val, ts=np.full(len(svc), ts))

    def point_edits(self, st, k, t_from, t_to, locked):
        """k точечных правок разных цен: 40% основная, остальное — прочие типы."""
        S, T, rng = self.S, self.T, self.rng
        svc = rng.integers(0, S, k)
        if T == 1:
            typ = np.zeros(k, dtype=np.int64)
        else:
            free = np.array([t for t in range(1, T) if t not in locked])
            typ = np.where(rng.random(k) < 0.4, 0, rng.choice(free, k))
            if 0 in locked:
                typ = np.where(typ == 0, rng.choice(free, k), typ)
        cur = st[typ, svc]
        base = st[0, svc]
        new = (cur * rng.uniform(0.9, 1.15, k) / 100).round().astype(np.int64) * 100
        new = np.where(cur < 0, (base * rng.uniform(0.8, 1.5, k) / 100).round().astype(np.int64) * 100, new)
        clear = (rng.random(k) < 0.03) & (typ != 0) & (cur >= 0)
        new = np.where(clear, EMPTY, new)
        ts = np.sort(rng.uniform(t_from, t_to, k))
        return svc, typ, new, ts

    def apply(self, st, v):
        st[v.typ, v.svc] = v.val

    def history_branch1(self, days, fill):
        rng, T = self.rng, self.T
        st = self.initial_state(fill)
        a = self.author()
        imp = self.full_version(1, st, self.day_ts(0, 9), a)
        self.versions.append(imp)

        # Ежемесячно (дни 30, 60, ...): запланированное +N% по всему прайсу одного
        # типа, создано за 5 дней до даты; пока висит, этот тип не правят.
        if self.profile == "single":
            # Авторедактирование в продукте — всегда по одному типу; все типы разом
            # меняются только восстановлением и применением к филиалу.
            monthly = {d: int(rng.integers(0, T)) for d in range(30, days, 30)}
            quarterly = set()
        else:
            monthly = {d: (d // 30 - 1) % T for d in range(30, days, 30)}
            quarterly = set(range(91, days, 91))

        for d in range(1, days):
            locked = {t for md, t in monthly.items() if md - 5 <= d < md}
            if d in monthly:
                t = monthly[d]
                p = int(rng.integers(3, 9))
                new = st.copy()
                new[t] = pct(st[t], p)
                v = self.full_version(1, new, self.day_ts(d), self.author(), types=[t])
                v.created_at, v.planned_at = self.day_ts(d - 5, 14), self.day_ts(d)
                self.versions.append(v)
                self.apply(st, v)

            # 1–3 сотрудника правят по очереди; дневная версия = (день, сотрудник).
            # Окна не пересекаются: правка вливается в дневную версию только пока
            # после неё не появилась версия с бОльшим номером.
            n_auth = int(rng.integers(1, 4))
            edits = int(rng.integers(10, 201))
            bounds = np.linspace(9, 18, n_auth + 1)
            for i in range(n_auth):
                k = max(1, edits // n_auth)
                svc, typ, val, ts = self.point_edits(st, k, self.day_ts(d, bounds[i]), self.day_ts(d, bounds[i + 1]), locked)
                if i == 0 and d in quarterly:
                    # Ежеквартально: массовое +N% «сразу» по всем типам, вливается
                    # в дневную версию первого сотрудника.
                    p = int(rng.integers(3, 9))
                    m = self.full_version(1, np.stack([pct(st[t], p) for t in range(T)]), self.day_ts(d, bounds[0]), 0)
                    svc, typ, val, ts = (np.concatenate([m.svc, svc]), np.concatenate([m.typ, typ]),
                                         np.concatenate([m.val, val]), np.concatenate([m.ts, ts]))
                svc, typ, val, ts = dedupe_last(svc, typ, val, ts, self.S)
                v = Version(1, self.author(), float(ts.min()), float(ts.min()), svc=svc, typ=typ, val=val, ts=ts)
                self.versions.append(v)
                self.apply(st, v)

        # Висящая запланированная версия: 500 пар +5% через 20 дней (для страницы
        # прайса, восстановления и отмены). Основной тип не трогаем, если есть другие.
        k = 500
        if T == 1:
            svc, typ = self.rng.choice(self.S, k, replace=False), np.zeros(k, dtype=np.int64)
        else:
            filled = np.argwhere(st[1:] >= 0)
            pick = filled[self.rng.choice(len(filled), k, replace=False)]
            typ, svc = pick[:, 0] + 1, pick[:, 1]
        svc, typ, val, _ = dedupe_last(svc, typ, pct(st[typ, svc], 5), np.zeros(k), self.S)
        created = self.day_ts(days - 1, 17)
        planned = (self.today + timedelta(days=20)).timestamp()
        self.versions.append(Version(1, self.author(), created, None, planned_at=planned,
                                     svc=svc, typ=typ, val=val, ts=np.full(len(svc), planned)))
        self.state1 = st

    def import_only(self, branch, fill):
        st = self.initial_state(fill)
        self.versions.append(self.full_version(branch, st, self.day_ts(0, 9), self.author()))

    # ------------------------------------------------------------ X и Y
    def write_models(self):
        S, T = self.S, self.T
        # Номера: по порядку применения; id версии: по порядку создания.
        applied = sorted((v for v in self.versions if v.applied_at is not None), key=lambda v: (v.applied_at, v.created_at))
        for n, v in enumerate(applied, 1):
            v.number = n
        for i, v in enumerate(sorted(self.versions, key=lambda v: v.created_at), 1):
            v.id = i
        last_num = {b: max(v.number for v in applied if v.branch == b) for b in (1, 2)}

        yv, xv = io.StringIO(), io.StringIO()
        yb, xb = io.StringIO(), io.StringIO()
        y_id = x_id = 0
        n_rows = sum(len(v.svc) for v in self.versions)
        y_next = np.full(n_rows + 1, -1, dtype=np.int64)
        y_status = {}
        y_meta = []  # (id, svc_id, type_id, version_id, ver, value) — status/next позже
        x_rows = []  # [id, svc_id, valid_from, price, json, child]
        state = {b: np.full((T, S), EMPTY, dtype=np.int64) for b in (1, 2)}
        last_y = {b: np.full((T, S), -1, dtype=np.int64) for b in (1, 2)}
        last_x = {b: np.full(S, -1, dtype=np.int64) for b in (1, 2)}

        fmt_ts = lambda e: datetime.fromtimestamp(e, TZ).isoformat()  # noqa: E731
        order = applied + [v for v in self.versions if v.applied_at is None]
        for v in order:
            b = v.branch
            tids, sids = self.type_ids[b], self.svc_ids[b]
            pending = v.applied_at is None
            # --- Y: строка на пару
            ids = np.arange(y_id + 1, y_id + 1 + len(v.svc))
            y_id += len(v.svc)
            if not pending:
                prev = last_y[b][v.typ, v.svc]
                has = prev >= 0
                y_next[prev[has]] = ids[has]
                last_y[b][v.typ, v.svc] = ids
            ver = "\\N" if pending else str(v.number)
            for i, s, t, val in zip(ids.tolist(), v.svc.tolist(), v.typ.tolist(), v.val.tolist()):
                y_meta.append((i, int(sids[s]), int(tids[t]), v.id, ver, "\\N" if val < 0 else f"{val / 100:.2f}"))
                if pending:
                    y_status[i] = "S"

            # --- X: строка на услугу с полным снимком после версии
            st = state[b] if pending is False else state[b].copy()
            st[v.typ, v.svc] = v.val
            order_ = np.lexsort((v.ts, v.svc))
            us, first = np.unique(v.svc[order_], return_index=True)
            vf = v.ts[order_][first]
            new_ids = np.arange(x_id + 1, x_id + 1 + len(us))
            x_id += len(us)
            for i, s, f in zip(new_ids.tolist(), us.tolist(), vf.tolist()):
                col = st[:, s]
                js = "{" + ", ".join(f'"{int(tids[t])}": {col[t] / 100:.2f}' for t in range(1, T) if col[t] >= 0) + "}"
                x_rows.append([i, int(sids[s]), fmt_ts(f), f"{col[0] / 100:.2f}", js, -1])
            if not pending:
                prev = last_x[b][us]
                for p_, n_ in zip(prev.tolist(), new_ids.tolist()):
                    if p_ >= 0:
                        x_rows[p_ - 1][5] = n_
                last_x[b][us] = new_ids
            v.x_row_ids = new_ids

            # --- версии
            status = "S" if pending else ("A" if v.number == last_num[b] else "O")
            planned = fmt_ts(v.planned_at) if v.planned_at else "\\N"
            yv.write(f"{v.id}\t{b}\t{ver}\t{v.author}\t{fmt_ts(v.created_at)}\t{status}\t{planned}\t\\N\n")
            last_change = float(v.ts.max())
            ptypes = ",".join(str(int(tids[t])) for t in np.unique(v.typ))
            rows = ",".join(map(str, new_ids.tolist()))
            xv.write(f"{v.id}\t{b}\t{v.author}\t{fmt_ts(v.created_at)}\t"
                     f"{datetime.fromtimestamp(v.created_at, TZ).date()}\t{status}\t{planned}\t"
                     f"{fmt_ts(last_change)}\t\\N\t{{{rows}}}\t{{{ptypes}}}\n")

        for i, s, t, vid, ver, val in y_meta:
            st_ = y_status.get(i) or ("O" if y_next[i] >= 0 else "A")
            nx = "\\N" if y_next[i] < 0 else str(y_next[i])
            yb.write(f"{i}\t{s}\t{t}\t{vid}\t{ver}\t{val}\t{st_}\t{nx}\n")
        for r in x_rows:
            child = "\\N" if r[5] < 0 else str(r[5])
            xb.write(f"{r[0]}\t{r[1]}\t{r[2]}\t{r[3]}\t{r[4]}\t{child}\n")

        with self.raw.cursor() as c:
            # FK-триггеры на время загрузки выключены: данные согласованы генератором.
            c.execute("SET session_replication_role = replica")
        self.copy_text("y_version", ["id", "branch_id", "number", "author_id", "created_at", "status",
                                     "planned_at", "restored_from_id"], yv)
        self.copy_text("y_value", ["id", "service_id", "price_type_id", "version_id", "ver", "value",
                                   "status", "next_ver_id"], yb)
        self.copy_text("x_version", ["id", "branch_id", "author_id", "created_at", "day", "status", "planned_at",
                                     "last_change_at", "restored_from_id", "row_ids", "price_type_ids"], xv)
        self.copy_text("x_row", ["id", "service_id", "valid_from", "price", "price_overrides", "child_id"], xb)
        with self.raw.cursor() as c:
            c.execute("SET session_replication_role = origin")
        self.stdout.write(f"  Y: {y_id} значений, X: {x_id} строк, версий: {len(self.versions)}")

    def finish(self):
        with self.raw.cursor() as c:
            for t in ("employee", "branch", "price_type", "category", "service",
                      "x_version", "x_row", "y_version", "y_value"):
                c.execute(f"SELECT setval(pg_get_serial_sequence('{t}', 'id'), (SELECT max(id) FROM {t}))")
            # Последовательность номеров версий Y: следующий номер при применении.
            c.execute("CREATE SEQUENCE IF NOT EXISTS y_version_number_seq")
            c.execute("SELECT setval('y_version_number_seq', (SELECT max(number) FROM y_version))")
            c.execute("VACUUM (ANALYZE, FREEZE)")
            c.execute("CHECKPOINT")
