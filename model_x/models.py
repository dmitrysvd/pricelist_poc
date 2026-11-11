"""Модель X: одна таблица состояний цен услуги, связный список через child.

Строка = полное состояние всех цен услуги начиная с valid_from: основная цена
в колонке price, остальные типы в JSON price_overrides {"<price_type_id>": цена}.
Отсутствие ключа в JSON = цена этого типа не задана.

Запланированная строка (valid_from в будущем) до применения НЕ привязана к цепочке:
у неё и у текущей строки child IS NULL. Применение по дате проставляет
current.child = scheduled. Поэтому актуальная = child IS NULL AND valid_from <= now().
"""
from django.contrib.postgres.indexes import BrinIndex
from django.contrib.postgres.fields import ArrayField
from django.db import models

from common.models import Branch, Employee, Service


class XRow(models.Model):
    service = models.ForeignKey(Service, models.CASCADE, db_index=False)
    valid_from = models.DateTimeField()
    price = models.DecimalField(max_digits=12, decimal_places=2)
    price_overrides = models.JSONField(default=dict)
    # OneToOne = FK + UNIQUE-индекс: он же B-tree по FK для перехода child -> parent.
    child = models.OneToOneField("self", models.SET_NULL, null=True, related_name="parent")

    class Meta:
        db_table = "x_row"
        indexes = [
            BrinIndex(fields=["valid_from"], name="x_row_valid_from_brin"),
            # B-tree по FK service: заодно упорядочен по valid_from, чтобы история
            # услуги читалась одним проходом индекса.
            models.Index(fields=["service", "valid_from"], name="x_row_service_vf_idx"),
            # Достройка: частичный индекс по хвостам цепочек (актуальные +
            # запланированные). Горячий путь и страница прайса идут по нему.
            models.Index(
                fields=["service", "valid_from"],
                include=["price"],
                condition=models.Q(child__isnull=True),
                name="x_row_tail_idx",
            ),
        ]


class XVersion(models.Model):
    CURRENT, OLD, SCHEDULED = "A", "O", "S"
    branch = models.ForeignKey(Branch, models.CASCADE)
    author = models.ForeignKey(Employee, models.PROTECT)
    created_at = models.DateTimeField()
    day = models.DateField()
    status = models.CharField(max_length=1)
    planned_at = models.DateTimeField(null=True)
    # Достройка: момент последней правки в версии. Срез «Прайс-лист от <версии>»
    # берётся на этот момент (valid_from <= last_change_at).
    last_change_at = models.DateTimeField()
    restored_from = models.ForeignKey("self", models.SET_NULL, null=True)
    # Связь только версия -> строки: id строк X, созданных в этой версии.
    row_ids = ArrayField(models.BigIntegerField(), default=list)
    # Достройка (согласовано): какие типы цен менялись в версии — для фильтра
    # истории по типу без diff JSON строк с их родителями.
    price_type_ids = ArrayField(models.IntegerField(), default=list)

    class Meta:
        db_table = "x_version"
        # Инвариант: у филиала ровно одна актуальная (последняя применённая) версия.
        # Без него две параллельные транзакции, открывающие версию, оставили бы две 'A'.
        # Он же — поиск актуальной версии филиала одним спуском по индексу.
        constraints = [
            models.UniqueConstraint(fields=["branch"], condition=models.Q(status="A"), name="x_version_current_uq"),
        ]
        indexes = [
            models.Index(fields=["branch", "created_at"], name="x_version_branch_created_idx"),
        ]
