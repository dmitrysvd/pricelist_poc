"""Модель Y2: Y без обновления старых записей (append-only значения).

Отличия от Y:
- нет status и next_ver у значения: запись изменения пары — один INSERT;
- актуальное значение пары = последняя запись с ver IS NOT NULL (по номеру),
  ищется спуском по индексу y2_value_latest_uq с LIMIT 1;
- старая цена относительно версии = предыдущая запись пары (ver < N, LIMIT 1);
- запланированное — запись с ver = NULL, применение проставляет ver.
"""
from django.db import models

from common.models import Branch, Employee, PriceType, Service


class Y2Version(models.Model):
    CURRENT, OLD, SCHEDULED = "A", "O", "S"
    branch = models.ForeignKey(Branch, models.CASCADE)
    number = models.BigIntegerField(null=True, unique=True)
    author = models.ForeignKey(Employee, models.PROTECT)
    created_at = models.DateTimeField()
    status = models.CharField(max_length=1)
    planned_at = models.DateTimeField(null=True)
    restored_from = models.ForeignKey("self", models.SET_NULL, null=True)

    class Meta:
        db_table = "y2_version"
        constraints = [
            # одна актуальная версия на филиал (как в X и Y)
            models.UniqueConstraint(fields=["branch"], condition=models.Q(status="A"), name="y2_version_current_uq"),
        ]
        indexes = [
            models.Index(fields=["branch", "created_at"], name="y2_version_branch_created_idx"),
        ]


class Y2Value(models.Model):
    service = models.ForeignKey(Service, models.CASCADE, db_index=False)
    price_type = models.ForeignKey(PriceType, models.CASCADE, db_index=False)
    version = models.ForeignKey(Y2Version, models.CASCADE, db_index=False, related_name="values")
    # номер версии; NULL — запланированная, ещё не применённая запись
    ver = models.BigIntegerField(null=True)
    # NULL — цена этого типа очищена в этой версии
    value = models.DecimalField(max_digits=12, decimal_places=2, null=True)

    class Meta:
        db_table = "y2_value"
        constraints = [
            # Последняя запись пары: обратный спуск по (service, type, ver) с LIMIT 1.
            # UNIQUE — у пары не больше одной записи в версии; INCLUDE (value) —
            # index-only scan (таблица append-only, visibility map остаётся чистой).
            models.UniqueConstraint(
                fields=["service", "price_type", "ver"],
                include=["value"],
                condition=models.Q(ver__isnull=False),
                name="y2_value_latest_uq",
            ),
            # Не больше одной запланированной записи на пару.
            models.UniqueConstraint(
                fields=["service", "price_type"],
                condition=models.Q(ver__isnull=True),
                name="y2_value_scheduled_uq",
            ),
        ]
        indexes = [
            # значения версии: применение, отмена, фильтр истории по типу
            models.Index(fields=["version", "price_type"], name="y2_value_version_idx"),
        ]
