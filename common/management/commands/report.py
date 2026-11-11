"""Сводка results/raw/*.json -> results/REPORT.md (таблицы «сценарий x модель»)."""
import json
import statistics
from collections import defaultdict
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from bench.runner import MODEL_TABLES, connect

RES = Path(settings.BASE_DIR) / "results"
DS_ORDER = ["t1", "t1s", "t5", "t5s", "t20", "t20s", "x10"]
SC_TITLE = {
    "1": "Горячий путь: запись на приём (основная + страховая)",
    "2": "Страница прайса (50 услуг, все типы + запланированные)",
    "3": "Массовое +10% по всему прайсу одного типа",
    "4": "Применение запланированной версии",
    "5": "Отмена запланированной версии",
    "6": "Точечная правка цены",
    "7": "Просмотр версии «Прайс-лист от <дата>»",
    "8": "История версий",
    "9": "Восстановление версии",
    "10": "Новый тип цены, заполнение −10% от основной",
    "11": "Применение прайса к другому филиалу",
    "12": "Импорт в пустой прайс",
}


def ms(v):
    if v is None:
        return "—"
    return f"{v:.2f}" if v < 10 else f"{v:.0f}"


def kb(v):
    if v is None:
        return "—"
    return f"{v / 1024:.0f} КБ" if abs(v) < 1024 * 1024 else f"{v / 1024 / 1024:.1f} МБ"


def ratio_note(x, y, what="быстрее", xname="X", yname="Y"):
    if not x or not y:
        return ""
    if max(x, y) / min(x, y) < 1.15:
        return "в пределах 15%"
    r = y / x if x < y else x / y
    k = f"{r:.1f}" if r < 100 else f"{r:.0f}"
    return f"{xname} {what} в {k}×" if x < y else f"{yname} {what} в {k}×"


class Command(BaseCommand):
    def add_arguments(self, p):
        p.add_argument("--out", default=str(RES / "REPORT.md"))

    def handle(self, *a, **o):
        rows = []
        for f in sorted((RES / "raw").glob("*.json")):
            # smoke — отладочные прогоны, wm — эксперимент с work_mem (в отдельном разделе)
            # _cold — прогоны с вытесненным кэшем (чтение меряет диск, а не модель)
            if "smoke" in f.name or "_wm" in f.name or "_cold" in f.name:
                continue
            rows += json.loads(f.read_text())
        # последний прогон побеждает
        best = {}
        for r in rows:
            best[(r["scenario"], r["variant"], r["dataset"], r["model"])] = r
        by_sc = defaultdict(lambda: defaultdict(dict))
        for (sc, var, ds, m), r in best.items():
            by_sc[sc][(var, ds)][m] = r

        out = ["# Стенд X/Y: сводка замеров", "",
               "Время — мс, p50 / p95 по прогонам (чтение: 20 прогонов, прогретый кэш; запись: 5 прогонов, "
               "каждый в транзакции с ROLLBACK, отложенные FK проверяются внутри замера). "
               "WAL — медиана по прогонам. Строки — вставлено/обновлено/удалено в таблицах модели за операцию. "
               "Δ размер — рост heap+индексов таблиц модели за первый прогон на свежем клоне.", ""]
        for sc in sorted(by_sc, key=int):
            out += [f"## {sc}. {SC_TITLE.get(sc, '')}", ""]
            write = any(r.get("wal_bytes") for v in by_sc[sc].values() for r in v.values())
            if write:
                out += ["| вариант | типов | X p50 / p95 | Y p50 / p95 | X строк (ins/upd/del) | Y строк | "
                        "X WAL | Y WAL | X Δ размер | Y Δ размер | вывод |",
                        "|---|---|---|---|---|---|---|---|---|---|---|"]
            else:
                out += ["| вариант | типов | X p50 / p95 | Y p50 / p95 | строк в ответе | вывод |",
                        "|---|---|---|---|---|---|"]
            keys = sorted(by_sc[sc], key=lambda k: (list(dict.fromkeys(v for v, _ in by_sc[sc])).index(k[0]),
                                                     DS_ORDER.index(k[1]) if k[1] in DS_ORDER else 99))
            expanded = []
            for var, ds in keys:
                ms_ = by_sc[sc][(var, ds)]
                expanded.append((var, ds, ms_.get("X", {}), ms_.get("Y", {}), "X", "Y"))
                for alt in ("X(б)", "X·lat"):
                    if alt in ms_:
                        expanded.append((f"{var} — {alt}", ds, ms_[alt], ms_.get("Y", {}), alt, "Y"))
                if "Y2" in ms_:
                    # Y2 (append-only Y) сравнивается с X того же прогона
                    expanded.append((f"{var} — Y2 против X", ds, ms_.get("X", {}), ms_["Y2"], "X", "Y2"))
            for var, ds, x, y, xname, yname in expanded:
                err = " ".join(f"{m}: ОШИБКА {r['error'][:80]}" for m, r in (("X", x), ("Y", y)) if r.get("error"))
                def t(r):
                    n = len(r.get("times_ms") or [])
                    if n and n < 5:  # p95 из <5 прогонов — это просто максимум
                        return f"{ms(r.get('p50'))} / max {ms(max(r['times_ms']))} (n={n})"
                    return f"{ms(r.get('p50'))} / {ms(r.get('p95'))}"
                tx, ty = t(x), t(y)
                note = err or ratio_note(x.get("p50"), y.get("p50"), xname=xname, yname=yname)
                if write:
                    def tup(r):
                        t = r.get("tuples") or {}
                        return f"{t.get('ins', 0)}/{t.get('upd', 0)}/{t.get('del', 0)}" if t else "—"

                    def wal(r):
                        return kb(statistics.median(r["wal_bytes"])) if r.get("wal_bytes") else "—"

                    def size(r):
                        d = r.get("size_delta") or {}
                        return kb(sum(v["total"] for v in d.values())) if d else "—"
                    wx = statistics.median(x["wal_bytes"]) if x.get("wal_bytes") else None
                    wy = statistics.median(y["wal_bytes"]) if y.get("wal_bytes") else None
                    if not err and wx and wy:
                        note += "; WAL: " + ratio_note(wx, wy, "меньше", xname=xname, yname=yname)
                    out.append(f"| {var} | {ds} | {tx} | {ty} | {tup(x)} | {tup(y)} | {wal(x)} | {wal(y)} | "
                               f"{size(x)} | {size(y)} | {note} |")
                else:
                    out.append(f"| {var} | {ds} | {tx} | {ty} | {x.get('rows_returned', '—')} | {note} |")
            out.append("")

        out += ["## Размер таблиц после года истории", "",
                "| типов | модель | строк | heap | индексы | всего |", "|---|---|---|---|---|---|"]
        size_rows = {}
        admin = connect("bench", autocommit=True)
        dbs = [r[0] for r in admin.execute("SELECT datname FROM pg_database WHERE datname ~ '^bench_(t|x)'")]
        admin.close()
        for ds in DS_ORDER:
            if f"bench_{ds}" not in dbs:
                continue
            c = connect(f"bench_{ds}")
            # X(б) и X·lat хранятся в таблицах X — их размер не отдельная строка
            for m, tables in ((k, v) for k, v in MODEL_TABLES.items() if k in ("X", "Y", "Y2", "общая")):
                r = c.execute("SELECT sum(c.reltuples)::bigint, sum(pg_relation_size(c.oid)), sum(pg_indexes_size(c.oid)), "
                              "sum(pg_total_relation_size(c.oid)) FROM pg_class c WHERE relname = ANY(%s)",
                              (tables,)).fetchone()
                out.append(f"| {ds} | {m} | {r[0]:,} | {kb(r[1])} | {kb(r[2])} | {kb(r[3])} |".replace(",", " "))
                size_rows[(ds, m)] = r[3]
            c.close()
        docs = Path(settings.BASE_DIR) / "docs"
        out += ["", (docs / "findings.md").read_text(), "", (docs / "assumptions.md").read_text()]
        Path(o["out"]).write_text("\n".join(out) + "\n")
        self.update_readme(by_sc, size_rows)
        self.stdout.write(self.style.SUCCESS(f"-> {o['out']}"))

    # ------------------------------------------------------------------ README
    README_ROWS = [
        ("1", "10 услуг", "1. Горячий путь, 10 услуг, мс"),
        ("2", "курсор в середине", "2. Страница прайса, мс"),
        ("2", "только без цены (страховая)", "2. Страница «без цены», мс"),
        ("3", "а) сразу, основная +10%", "3а. +10% сразу, 85 тыс., мс"),
        ("3", "б) запланированно, основная +10%", "3б. +10% по плану, мс"),
        ("4", "применение запланированной (основная, весь прайс)", "4. Применение плана, мс"),
        ("5", "отмена запланированной (основная, весь прайс)", "5. Отмена плана, мс"),
        ("6", "1 правка (открывает дневную версию)", "6. Точечная правка, мс"),
        ("7", "дневная версия, ~полгода назад", "7. Версия (дневная), мс"),
        ("7", "массовая версия, ~полгода назад", "7. Версия (массовая), мс"),
        ("8", "история: месяц + сотрудник + тип", "8. История с фильтрами, мс"),
        ("8", "следующее изменение пары после V", "8. След. изменение пары, мс"),
        ("9", "восстановление версии ~полгода назад, все типы", "9. Восстановление, мс"),
        ("10", "новый тип = основная −10%", "10. Новый тип −10%, мс"),
        ("11", "прайс филиала 1 -> филиал 2 (те же услуги)", "11. Прайс A→B, мс"),
        ("12", "импорт 85 тыс. услуг в пустой прайс", "12. Импорт, мс"),
    ]

    def update_readme(self, by_sc, sizes):
        """Компактная таблица «сценарий x датасет» (p50, X / Y) в README между маркерами."""
        dss = [d for d in DS_ORDER if any(d == k[1] for sc in by_sc.values() for k in sc)]
        head = "| сценарий | " + " | ".join(dss) + " |"
        lines = ["Ячейка — p50 «X / Y» или «X / Y / Y2» (Y2 — Y без обновления старых записей). Профиль истории: t1/t5/t20 — исходный (ежемесячно один тип + "
                 "ежеквартально все типы), t1s/t5s/t20s — только по одному типу. Полные таблицы с p95, "
                 "строками, WAL, Δ размера и выводами — `results/REPORT.md`.", "",
                 head, "|" + "---|" * (len(dss) + 1)]
        for sc, var, title in self.README_ROWS:
            cells = []
            for ds in dss:
                r = by_sc.get(sc, {}).get((var, ds), {})
                x, y, y2 = (r.get(m, {}).get("p50") for m in ("X", "Y", "Y2"))
                cell = f"{ms(x)} / {ms(y)}" if x or y else "—"
                cells.append(cell + (f" / {ms(y2)}" if y2 else ""))
            if any(c != "—" for c in cells):
                lines.append(f"| {title} | " + " | ".join(cells) + " |")
        cells = [(f"{kb(sizes.get((ds, 'X')))} / {kb(sizes.get((ds, 'Y')))}"
                  + (f" / {kb(sizes[(ds, 'Y2')])}" if (ds, "Y2") in sizes else "")) if (ds, "X") in sizes else "—"
                 for ds in dss]
        lines.append("| Размер после года (heap+индексы) | " + " | ".join(cells) + " |")
        readme = Path(settings.BASE_DIR) / "README.md"
        text = readme.read_text()
        a, b = "<!-- summary:start -->", "<!-- summary:end -->"
        block = a + "\n" + "\n".join(lines) + "\n" + b
        text = text[:text.index(a)] + block + text[text.index(b) + len(b):] if a in text else text + "\n## Сводка\n\n" + block + "\n"
        readme.write_text(text)
