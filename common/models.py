"""Общая часть стенда: одинакова для обеих моделей, замеряется только как фон.

Типы цен вынесены сюда, потому что они нужны обеим моделям: Y ссылается на них
FK из значений цен, X использует id типа как ключ в JSON price_overrides.
"""
from django.db import models


class Branch(models.Model):
    name = models.CharField(max_length=200)

    class Meta:
        db_table = "branch"


class Employee(models.Model):
    name = models.CharField(max_length=200)

    class Meta:
        db_table = "employee"


class PriceType(models.Model):
    MAIN, INSURANCE, CONTRACTOR, ZP, CUSTOM = "main", "ins", "contr", "zp", "custom"
    branch = models.ForeignKey(Branch, models.CASCADE)
    kind = models.CharField(max_length=10)
    name = models.CharField(max_length=200)
    sort = models.IntegerField(default=0)

    class Meta:
        db_table = "price_type"


class Category(models.Model):
    branch = models.ForeignKey(Branch, models.CASCADE)
    name = models.CharField(max_length=300)
    # Порядок показа категорий. Услуги сортируются по нему, а не по имени
    # категории, чтобы курсорная пагинация шла по одному индексу services.
    sort = models.IntegerField()

    class Meta:
        db_table = "category"


class Service(models.Model):
    branch = models.ForeignKey(Branch, models.CASCADE)
    category = models.ForeignKey(Category, models.CASCADE)
    # Денормализованный Category.sort: без него сортировка «категория-код-название»
    # требует join+sort всех 85 тыс. услуг на каждую страницу.
    category_sort = models.IntegerField()
    code = models.CharField(max_length=50)
    name = models.CharField(max_length=500)
    archived = models.BooleanField(default=False)

    class Meta:
        db_table = "service"
        indexes = [
            models.Index(
                fields=["branch", "category_sort", "code", "name", "id"],
                condition=models.Q(archived=False),
                name="service_page_idx",
            ),
        ]
