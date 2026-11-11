"""Вариант горячего пути для моделей с хранением по парам (Y, Y3): «квадрат»
услуги x типы одним сканом индекса актуальных (service_id = ANY AND price_type_id = ANY),
пивот в SQL через GROUP BY + FILTER — одна строка на услугу, встраивается JOIN-ом.
Замеряется отдельной строкой в сц. 1."""
from bench.iface import Op, Step
from model_y.impl import YModel
from model_y3.impl import Y3Model

SQ = """
-- Горячий путь «квадратом»: все пары (услуги x {{основная, страховая}}) одним сканом
-- индекса актуальных с двумя условиями = ANY; пивот в SQL — одна строка на услугу.
SELECT s.id, s.code, s.name, p.main_price, p.ins_price
FROM service s
JOIN (
  SELECT v.service_id,
         max(v.value) FILTER (WHERE v.price_type_id = %(main)s) AS main_price,
         max(v.value) FILTER (WHERE v.price_type_id = %(ins)s) AS ins_price
  FROM {table} v
  WHERE {actual} v.service_id = ANY(%(ids)s) AND v.price_type_id = ANY(%(types)s)
  GROUP BY v.service_id
) p ON p.service_id = s.id
WHERE s.id = ANY(%(ids)s) AND p.main_price IS NOT NULL
"""


def square(table, actual):
    def current_prices(self, service_ids, main_type, ins_type):
        types = [main_type] + ([ins_type] if ins_type else [])
        sql = SQ.format(table=table, actual=actual)
        return Op([Step("q", sql, {"ids": service_ids, "main": main_type, "ins": ins_type, "types": types})])
    return current_prices


class YModelSq(YModel):
    name = "Y·кв"
    only_scenarios = {"1"}
    current_prices = square("y_value", "v.status = 'A' AND")


class Y3ModelSq(Y3Model):
    name = "Y3·кв"
    only_scenarios = {"1"}
    current_prices = square("y3_current", "")
