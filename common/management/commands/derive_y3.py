"""Заполнить таблицы Y3 из Y той же базы: история как у Y2 + y3_current из актуальных записей Y.

uv run python manage.py derive_y3 --database t5
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
            c.execute("TRUNCATE y3_current, y3_value, y3_version")
            c.execute("SET session_replication_role = replica")  # данные уже согласованы в Y
            c.execute("""INSERT INTO y3_version (id, branch_id, number, author_id, created_at, status, planned_at,
                                                 restored_from_id)
                         SELECT id, branch_id, number, author_id, created_at, status, planned_at, restored_from_id
                         FROM y_version ORDER BY id""")
            # история в том же порядке, что грузил генератор: индексы растут как у Y и Y2
            c.execute("""INSERT INTO y3_value (id, service_id, price_type_id, version_id, ver, value)
                         SELECT id, service_id, price_type_id, version_id, ver, value FROM y_value ORDER BY id""")
            c.execute("""INSERT INTO y3_current (service_id, price_type_id, value, ver)
                         SELECT service_id, price_type_id, value, ver FROM y_value WHERE status = 'A'
                         ORDER BY service_id, price_type_id""")
            c.execute("SET session_replication_role = origin")
            for t in ("y3_version", "y3_value"):
                c.execute(f"SELECT setval(pg_get_serial_sequence('{t}', 'id'), (SELECT max(id) FROM {t}))")
            c.execute("CREATE SEQUENCE IF NOT EXISTS y3_version_number_seq")
            c.execute("SELECT setval('y3_version_number_seq', (SELECT max(number) FROM y3_version))")
            c.execute("VACUUM (ANALYZE, FREEZE) y3_version, y3_value, y3_current")
            c.execute("CHECKPOINT")
            c.execute("""SELECT (SELECT count(*) FROM y3_current),
                                pg_size_pretty(pg_total_relation_size('y3_current')),
                                pg_size_pretty(pg_total_relation_size('y3_value'))""")
            n, sc, sv = c.fetchone()
        self.stdout.write(self.style.SUCCESS(f"{o['database']}: y3_current {n} пар ({sc}), история {sv}, "
                                             f"{time.time() - t0:.0f}s"))
