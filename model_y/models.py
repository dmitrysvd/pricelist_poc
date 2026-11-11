"""Модель Y: версии + значения цен, хранятся только изменения пар услуга x тип.

Значение ссылается на версию двумя полями:
- version_id (достройка, согласовано) — FK на id версии, заполнен всегда;
  нужен, потому что у запланированной версии номера ещё нет;
- ver — номер версии, NULL пока версия запланирована, проставляется при применении.
next_ver — FK на СЛЕДУЮЩУЮ ЗАПИСЬ значения той же пары (id), проставляется тем же
UPDATE, что снимает со старой записи статус «актуальная».
"""
from django.db import models

from common.models import Branch, Employee, PriceType, Service


class YVersion(models.Model):
    CURRENT, OLD, SCHEDULED = "A", "O", "S"
    branch = models.ForeignKey(Branch, models.CASCADE)
    number = models.BigIntegerField(null=True, unique=True)
    author = models.ForeignKey(Employee, models.PROTECT)
    created_at = models.DateTimeField()
    status = models.CharField(max_length=1)
    planned_at = models.DateTimeField(null=True)
    restored_from = models.ForeignKey("self", models.SET_NULL, null=True)

    class Meta:
        db_table = "y_version"
        # Инвариант: у филиала ровно одна актуальная (последняя применённая) версия.
        # Без него две параллельные транзакции, открывающие версию, оставили бы две 'A'.
        # Он же — поиск актуальной версии филиала одним спуском по индексу.
        constraints = [
            models.UniqueConstraint(fields=["branch"], condition=models.Q(status="A"), name="y_version_current_uq"),
        ]
        indexes = [
            models.Index(fields=["branch", "created_at"], name="y_version_branch_created_idx"),
        ]


class YValue(models.Model):
    ACTUAL, OLD, SCHEDULED = "A", "O", "S"
    service = models.ForeignKey(Service, models.CASCADE, db_index=False)
    price_type = models.ForeignKey(PriceType, models.CASCADE, db_index=False)
    version = models.ForeignKey(YVersion, models.CASCADE, db_index=False, related_name="values")
    ver = models.BigIntegerField(null=True)
    # NULL = цена этого типа очищена в этой версии (для основного типа не бывает).
    value = models.DecimalField(max_digits=12, decimal_places=2, null=True)
    status = models.CharField(max_length=1)
    # OneToOne = FK + UNIQUE-индекс: он же B-tree для перехода к предыдущей записи.
    next_ver = models.OneToOneField("self", models.SET_NULL, null=True, related_name="prev")

    class Meta:
        db_table = "y_value"
        constraints = [
            # Инвариант: максимум одна актуальная запись на пару. Он же — частичный
            # индекс по актуальным для горячего пути (INCLUDE value: index-only scan).
            models.UniqueConstraint(
                fields=["service", "price_type"],
                include=["value"],
                condition=models.Q(status="A"),
                name="y_value_actual_uq",
            ),
            # Инвариант: максимум одна запланированная запись на пару.
            models.UniqueConstraint(
                fields=["service", "price_type"],
                condition=models.Q(status="S"),
                name="y_value_scheduled_uq",
            ),
        ]
        indexes = [
            # Срезы: LATERAL ver <= N ORDER BY ver DESC LIMIT 1.
            models.Index(fields=["service", "price_type", "ver"], name="y_value_slice_idx"),
            # Достройка: значения версии — применение/отмена/восстановление
            # и фильтр истории по типу цены.
            models.Index(fields=["version", "price_type"], name="y_value_version_idx"),
        ]
