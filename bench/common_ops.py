"""Общие (не зависящие от модели) шаги сценариев 10–12.

Каждая модель выполняет их в своей транзакции перед ценовой частью, чтобы цены
ссылались на реальные новые id. Отдельно они замеряются псевдомоделью «общая»
(CommonOnly), чтобы ценовую часть X и Y можно было сравнить без общего фона.
"""
from bench.iface import Op, Step

# Сц.10: новый тип цены (таблица типов общая для X и Y).
ADD_TYPE = Step("ptype", """
INSERT INTO price_type (branch_id, kind, name, sort)
SELECT %(b)s, 'custom', 'Новый тип', coalesce(max(sort), 0) + 1 FROM price_type WHERE branch_id = %(b)s
RETURNING id, id::text AS key
""")

# Сц.11: у филиала-получателя те же услуги, сопоставление A->B 1:1 уже есть в
# продукте (здесь — таблица service_map, создаётся при подготовке клона, не замеряется).
SERVICE_MAP_SETUP = """
DROP TABLE IF EXISTS service_map;
CREATE TABLE service_map AS
SELECT a.id AS a_id, b.id AS b_id FROM service a JOIN service b ON b.id = a.id + %(n)s
WHERE a.branch_id = %(src)s AND b.branch_id = %(dst)s;
ALTER TABLE service_map ADD PRIMARY KEY (a_id);
CREATE UNIQUE INDEX ON service_map (b_id);
ANALYZE service_map;
"""

# Типы цен A -> B сопоставляются по порядку (sort); типы A без пары у B не переносятся.
TYPE_MAP = """tm AS (
  SELECT a.id AS a_t, b.id AS b_t FROM price_type a
  JOIN price_type b ON b.branch_id = %(dst)s AND b.sort = a.sort
  WHERE a.branch_id = %(src)s
)"""

# Сц.12: категории и услуги из import_staging (распарсенный xlsx: category, code, name, price).
IMPORT = [
    Step("cats", """
INSERT INTO category (branch_id, name, sort)
SELECT %(b)s, category, row_number() OVER (ORDER BY category) FROM (SELECT DISTINCT category FROM import_staging) c
"""),
    Step("svcs", """
INSERT INTO service (branch_id, category_id, category_sort, code, name, archived)
SELECT %(b)s, c.id, c.sort, st.code, st.name, false
FROM import_staging st JOIN category c ON c.branch_id = %(b)s AND c.name = st.category
"""),
]

IMPORT_STAGING_SETUP = """
DROP TABLE IF EXISTS import_staging;
CREATE TABLE import_staging AS
SELECT 'Категория ' || lpad((1 + (i * 7919) %% 1000)::text, 4, '0') AS category,
       'I' || lpad(i::text, 6, '0') AS code,
       'Импортированная услуга ' || i AS name,
       (500 + (i::bigint * 104729) %% 49500)::numeric(12,2) AS price
FROM generate_series(1, %(n)s) i;
ANALYZE import_staging;
"""


class CommonOnly:
    """Псевдомодель «общая»: только общие шаги, метрики по service/category."""
    name = "общая"

    def add_price_type(self, branch, author, main_type, pct_of_main):
        return Op([Step(ADD_TYPE.name, ADD_TYPE.sql, {"b": branch})])

    def import_prices(self, branch, author):
        return Op([Step(s.name, s.sql, {"b": branch}) for s in IMPORT], explain=1)
