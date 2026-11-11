"""Заполнить таблицы Y2 из Y той же базы: те же версии и записи, без status/next_ver.

Записи вставляются в порядке id (как их грузил генератор) в таблицу с уже
созданными индексами — индексы растут так же, как у Y, размеры сравнимы.
uv run python manage.py derive_y2 --database t5
"""
import time

from django.core.management.base import BaseCommand
from django.db import connections


class Command(BaseCommand):
    def add_arguments(self, p):
        p.add_argument("--database", required=True)

    def handle(self, *a, **o):
        t0 = time.time()
        conn = connections[o["database"]]
        conn.ensure_connection()
        raw = conn.connection
        raw.autocommit = True
        with raw.cursor() as c:
            c.execute("TRUNCATE y2_value, y2_version")
            c.execute("SET session_replication_role = replica")  # данные уже согласованы в Y
            c.execute("""INSERT INTO y2_version (id, branch_id, number, author_id, created_at, status, planned_at,
                                                 restored_from_id)
                         SELECT id, branch_id, number, author_id, created_at, status, planned_at, restored_from_id
                         FROM y_version ORDER BY id""")
            c.execute("""INSERT INTO y2_value (id, service_id, price_type_id, version_id, ver, value)
                         SELECT id, service_id, price_type_id, version_id, ver, value FROM y_value ORDER BY id""")
            c.execute("SET session_replication_role = origin")
            for t in ("y2_version", "y2_value"):
                c.execute(f"SELECT setval(pg_get_serial_sequence('{t}', 'id'), (SELECT max(id) FROM {t}))")
            c.execute("CREATE SEQUENCE IF NOT EXISTS y2_version_number_seq")
            c.execute("SELECT setval('y2_version_number_seq', (SELECT max(number) FROM y2_version))")
            c.execute("VACUUM (ANALYZE, FREEZE) y2_version, y2_value")
            c.execute("CHECKPOINT")
            c.execute("SELECT count(*), pg_size_pretty(pg_total_relation_size('y2_value')), "
                      "pg_size_pretty(pg_total_relation_size('y_value')) FROM y2_value")
            n, s2, s1 = c.fetchone()
        self.stdout.write(self.style.SUCCESS(f"{o['database']}: y2_value {n} строк, {s2} (y_value {s1}), "
                                             f"{time.time() - t0:.0f}s"))
