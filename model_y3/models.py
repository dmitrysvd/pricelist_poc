"""Модель Y3: история изменений как в Y2 (append-only) + таблица «текущее».

y3_value   — история: строка на изменение пары, старые записи не трогаются (как Y2);
y3_current — одна строка на пару (service, type): актуальные value и ver.
Изменение цены = INSERT в историю + UPSERT в y3_current. В y3_current единственный
индекс — первичный ключ (service_id, price_type_id), а value и ver в него не входят,
поэтому обновление HOT: новая версия строки ложится в ту же страницу, индекс не
меняется. fillfactor=80 оставляет на страницах место под такие обновления.
"""
from django.db import models

from common.models import Branch, Employee, PriceType, Service


class Y3Version(models.Model):
    branch = models.ForeignKey(Branch, models.CASCADE)
    number = models.BigIntegerField(null=True, unique=True)
    author = models.ForeignKey(Employee, models.PROTECT)
    created_at = models.DateTimeField()
    status = models.CharField(max_length=1)
    planned_at = models.DateTimeField(null=True)
    restored_from = models.ForeignKey("self", models.SET_NULL, null=True)

    class Meta:
        db_table = "y3_version"
        constraints = [
            models.UniqueConstraint(fields=["branch"], condition=models.Q(status="A"), name="y3_version_current_uq"),
        ]
        indexes = [models.Index(fields=["branch", "created_at"], name="y3_version_branch_created_idx")]


class Y3Value(models.Model):
    """История — та же схема и индексы, что у y2_value."""
    service = models.ForeignKey(Service, models.CASCADE, db_index=False)
    price_type = models.ForeignKey(PriceType, models.CASCADE, db_index=False)
    version = models.ForeignKey(Y3Version, models.CASCADE, db_index=False, related_name="values")
    ver = models.BigIntegerField(null=True)
    value = models.DecimalField(max_digits=12, decimal_places=2, null=True)

    class Meta:
        db_table = "y3_value"
        constraints = [
            models.UniqueConstraint(fields=["service", "price_type", "ver"], include=["value"],
                                    condition=models.Q(ver__isnull=False), name="y3_value_latest_uq"),
            models.UniqueConstraint(fields=["service", "price_type"], condition=models.Q(ver__isnull=True),
                                    name="y3_value_scheduled_uq"),
        ]
        indexes = [models.Index(fields=["version", "price_type"], name="y3_value_version_idx")]


class Y3Current(models.Model):
    """Актуальное значение пары: одна строка на (service, price_type)."""
    pk = models.CompositePrimaryKey("service", "price_type")
    service = models.ForeignKey(Service, models.CASCADE, db_index=False)
    price_type = models.ForeignKey(PriceType, models.CASCADE, db_index=False)
    value = models.DecimalField(max_digits=12, decimal_places=2, null=True)
    ver = models.BigIntegerField()

    class Meta:
        db_table = "y3_current"
