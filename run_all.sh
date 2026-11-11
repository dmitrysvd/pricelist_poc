#!/usr/bin/env bash
# Полный прогон стенда одной командой: ./run_all.sh
#   DATASETS="t1 t5 t20"  — какие датасеты (x10 — 850 тыс. услуг, 5 типов, добавить вручную)
#   SCENARIOS=1,...,12    — какие сценарии
#   SKIP_GEN=1            — не пересоздавать датасеты
set -euo pipefail
cd "$(dirname "$0")"

DATASETS=${DATASETS:-"t1 t5 t20"}
SCENARIOS=${SCENARIOS:-1,2,3,4,5,6,7,8,9,10,11,12}
export PGHOST="$PWD/.pgsock" PGUSER=bench PGPASSWORD=bench

# Postgres 18 в docker; сокет проброшен в ./.pgsock (без TCP).
if ! docker ps --format '{{.Names}}' | grep -qx pricelist-pg; then
  mkdir -p .pgsock && chmod 777 .pgsock
  docker start pricelist-pg 2>/dev/null || docker run -d --name pricelist-pg --shm-size=4g \
    -e POSTGRES_PASSWORD=bench -e POSTGRES_USER=bench -e POSTGRES_DB=bench \
    -v pricelist_pgdata:/var/lib/postgresql -v "$PWD/.pgsock:/var/run/postgresql" \
    postgres:18 \
    -c shared_buffers=8GB -c effective_cache_size=24GB -c work_mem=64MB -c maintenance_work_mem=2GB \
    -c max_wal_size=32GB -c checkpoint_timeout=30min -c track_io_timing=on \
    -c shared_preload_libraries=pg_stat_statements -c jit=off -c random_page_cost=1.1 \
    -c max_parallel_workers_per_gather=4 -c listen_addresses=''
  until psql -d bench -Xqc 'select 1' >/dev/null 2>&1; do sleep 1; done
fi

uv sync -q
types_of() { case $1 in t1) echo 1;; t5) echo 5;; t20) echo 20;; x10) echo 5;; esac; }
services_of() { case $1 in x10) echo 850000;; *) echo 85000;; esac; }
categories_of() { case $1 in x10) echo 10000;; *) echo 1000;; esac; }

for ds in $DATASETS; do
  if [[ -z "${SKIP_GEN:-}" ]]; then
    psql -d bench -Xqc "DROP DATABASE IF EXISTS bench_$ds" -c "CREATE DATABASE bench_$ds"
    uv run python manage.py migrate --database="$ds" -v0
    uv run python manage.py gen --database "$ds" --types "$(types_of "$ds")" \
      --services "$(services_of "$ds")" --categories "$(categories_of "$ds")"
  fi
done

list=$(echo $DATASETS | tr ' ' ',')
# Сверка: X и Y на одних параметрах дают одинаковый результат (чтение и запись).
uv run python manage.py verify --datasets "$list"
uv run python manage.py verify_writes --datasets "$list"
uv run python manage.py bench --datasets "$list" --scenarios "$SCENARIOS" --tag all
uv run python manage.py report
echo "Готово: results/REPORT.md"
