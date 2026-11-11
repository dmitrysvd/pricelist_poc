"""Общий интерфейс моделей прайс-листа: одна операция продукта = один метод.

Метод не выполняет SQL, а возвращает Op — список шагов одной транзакции.
Выполняет и замеряет их общий раннер (bench/runner.py), поэтому для обеих
моделей одинаково снимаются время, EXPLAIN, WAL и счётчики строк.

Версии адресуются логическим id: генератор выдаёт X и Y версиям одной ленты
одинаковые id, так что «версия 123» в X и в Y — одно и то же событие.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class Ref:
    """Значение колонки из первой строки результата шага `step` этой же Op."""
    step: str
    col: str


@dataclass
class Step:
    name: str
    sql: str
    params: dict = field(default_factory=dict)


@dataclass
class Op:
    steps: list[Step]
    # Индекс шага, для которого снимается EXPLAIN (ANALYZE, BUFFERS).
    explain: int = 0


@dataclass
class Edit:
    service_id: int
    price_type_id: int
    value: str | None  # None = очистить цену


class PriceModel(ABC):
    name: str

    # --- чтение -----------------------------------------------------------
    @abstractmethod
    def current_prices(self, service_ids: list[int], main_type: int, ins_type: int | None) -> Op:
        """Сц.1: актуальная основная + цена страховой для услуг записи на приём."""

    @abstractmethod
    def price_page(self, branch: int, cursor: tuple | None, limit: int, type_ids: list[int],
                   missing_type: int | None = None) -> Op:
        """Сц.2: страница прайса, актуальные цены всех типов + запланированные."""

    @abstractmethod
    def version_page(self, branch: int, version_id: int, cursor: tuple | None, limit: int,
                     type_ids: list[int]) -> Op:
        """Сц.7: «Прайс-лист от <дата>» — срез на версию + флаг изменения + старая цена."""

    @abstractmethod
    def history(self, branch: int, date_from: datetime | None, date_to: datetime | None,
                author: int | None, price_type: int | None, limit: int) -> Op:
        """Сц.8а: список версий с фильтрами."""

    @abstractmethod
    def next_change(self, service_id: int, price_type_id: int, version_id: int, is_main: bool) -> Op:
        """Сц.8б: следующее изменение цены пары услуга x тип после версии."""

    # --- запись -----------------------------------------------------------
    @abstractmethod
    def mass_change(self, branch: int, author: int, price_type_id: int, is_main: bool, pct: int,
                    planned_at: datetime | None) -> Op:
        """Сц.3: +pct% по всему прайсу одного типа; сразу (planned_at=None) или по плану."""

    @abstractmethod
    def apply_scheduled(self, branch: int, version_id: int) -> Op:
        """Сц.4: применение запланированной версии при наступлении даты."""

    @abstractmethod
    def cancel_scheduled(self, branch: int, version_id: int) -> Op:
        """Сц.5: отмена запланированной версии."""

    @abstractmethod
    def point_edit(self, branch: int, author: int, edit: Edit, main_type: int) -> Op:
        """Сц.6: одна правка цены в дневную версию (создаёт её или вливается)."""

    @abstractmethod
    def restore(self, branch: int, author: int, version_id: int, type_ids: list[int]) -> Op:
        """Сц.9: новая версия = срез версии version_id по всем типам; запланированные отменяются."""

    @abstractmethod
    def add_price_type(self, branch: int, author: int, main_type: int, pct_of_main: int) -> Op:
        """Сц.10: новый тип цены, заполнение pct_of_main% от основной по всему прайсу."""

    @abstractmethod
    def copy_branch(self, src: int, dst: int, author: int) -> Op:
        """Сц.11: прайс филиала src полностью заменяет услуги, категории и цены dst."""

    @abstractmethod
    def import_prices(self, branch: int, author: int) -> Op:
        """Сц.12: импорт из import_staging в пустой прайс филиала (только основная цена)."""
