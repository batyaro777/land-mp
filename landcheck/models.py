"""Доменные модели: заявка брокера, участок из НСПД с прослеживаемостью, флаги и вердикт."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Generic, TypeVar

T = TypeVar("T")

SOTKA_M2 = 100.0  # 1 сотка = 100 м²


class Purpose(StrEnum):
    """Цель покупателя. Значения совпадают с аргументом CLI --purpose."""

    IZHS = "izhs"  # построить жилой дом для постоянного проживания
    LPH = "lph"  # личное подсобное хозяйство: огород, животные, птица
    SNT = "snt"  # дача: садовый дом, огород, сезонное проживание
    FARMING = "farming"  # сельхозпроизводство: культуры, скот, ферма (КФХ)
    RECREATION = "recreation"  # рекреация, туризм
    COMMERCE = "commerce"  # торговля, услуги, офисы
    INDUSTRY = "industry"  # производство, склады
    MULTIFAMILY = "multifamily"  # многоквартирный дом, таунхаусы
    ANY = "any"  # цель не задана — только проверка данных

    @property
    def label(self) -> str:
        """Короткая подпись для таблиц и выгрузок."""
        return PURPOSE_LABELS[self]

    @property
    def activity(self) -> str:
        """Цель словами покупателя: что он хочет делать на участке (запрос брокеров 13.10)."""
        return PURPOSE_ACTIVITY[self]

    @property
    def residential(self) -> bool:
        """Цель предполагает жильё — для жилья действуют санитарные и зональные запреты."""
        return self in (Purpose.IZHS, Purpose.LPH, Purpose.SNT, Purpose.MULTIFAMILY)


PURPOSE_LABELS: dict[Purpose, str] = {
    Purpose.IZHS: "Жилой дом (ИЖС)",
    Purpose.LPH: "Личное хозяйство (ЛПХ)",
    Purpose.SNT: "Дача, садоводство",
    Purpose.FARMING: "Сельхозпроизводство, ферма",
    Purpose.RECREATION: "Отдых, туризм",
    Purpose.COMMERCE: "Торговля, услуги, офис",
    Purpose.INDUSTRY: "Производство, склад",
    Purpose.MULTIFAMILY: "Многоквартирный дом",
    Purpose.ANY: "Без цели",
}

PURPOSE_ACTIVITY: dict[Purpose, str] = {
    Purpose.IZHS: "Построить жилой дом для постоянного проживания",
    Purpose.LPH: "Вести личное хозяйство: огород, животные, птица",
    Purpose.SNT: "Дача: садовый дом, огород, сезонное проживание",
    Purpose.FARMING: "Выращивать культуры, разводить скот, ферма (КФХ)",
    Purpose.RECREATION: "База отдыха, глэмпинг, туризм",
    Purpose.COMMERCE: "Магазин, кафе, офис, услуги",
    Purpose.INDUSTRY: "Производство, склад, завод",
    Purpose.MULTIFAMILY: "Построить многоквартирный дом или таунхаусы",
    Purpose.ANY: "Только проверить данные участка",
}


class Level(StrEnum):
    """Уровень флага. Порядок важен: стоп > проверить > ок."""

    STOP = "стоп"
    CHECK = "проверить"
    OK = "ок"

    @property
    def rank(self) -> int:
        return {Level.STOP: 2, Level.CHECK: 1, Level.OK: 0}[self]


@dataclass(frozen=True)
class Sourced(Generic[T]):
    """Значение с источником и моментом получения — требование прослеживаемости ТЗ."""

    value: T
    source: str
    fetched_at: datetime

    def __bool__(self) -> bool:  # pragma: no cover — удобство в условиях
        return self.value is not None


@dataclass
class Listing:
    """Строка подборки брокера: кадастровый номер и то, что заявлено в объявлении."""

    cad_num: str
    price_rub: float | None = None
    declared_area_m2: float | None = None
    url: str | None = None
    note: str | None = None
    raw_cad_num: str | None = None  # как было во входе до нормализации

    @property
    def declared_area_sotka(self) -> float | None:
        if self.declared_area_m2 is None:
            return None
        return self.declared_area_m2 / SOTKA_M2

    @property
    def price_per_sotka(self) -> float | None:
        if self.price_rub is None or not self.declared_area_m2:
            return None
        return self.price_rub / (self.declared_area_m2 / SOTKA_M2)


@dataclass
class ZoneHit:
    """Пересечение участка с зоной (ЗОУИТ или территориальная зона).

    Поля по реальному ответу НСПД: ``type_zone`` → kind, ``name_by_doc`` → name,
    ``reg_numb_border`` → reg_number, ``content_restrict_encumbrances`` → restrictions.
    """

    layer: str  # название слоя НСПД
    reg_number: str | None = None
    kind: str | None = None
    name: str | None = None
    restrictions: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def layer_short(self) -> str:
        lo = self.layer.lower()
        if "особыми условиями" in lo or "зоуит" in lo:
            return "ЗОУИТ"
        if "территориальн" in lo:
            return "терзона"
        return self.layer

    def describe(self, max_len: int = 120) -> str:
        """Одна строка для таблицы: вид · название (реестровый номер)."""
        parts = [p for p in (self.kind, self.name) if p]
        text = " · ".join(parts) if parts else self.layer_short
        if self.reg_number:
            text = f"{text} ({self.reg_number})"
        if len(text) > max_len:
            text = text[: max_len - 1] + "…"
        return text


@dataclass
class Parcel:
    """Нормализованный участок из НСПД. Каждое поле знает, откуда оно."""

    cad_num: str
    found: bool
    fetched_at: datetime
    source: str = "НСПД, слой «Земельные участки из ЕГРН»"
    address: str | None = None
    status: str | None = None
    category: str | None = None
    permitted_use: str | None = None
    permitted_use_tab: list[str] = field(default_factory=list)
    area_m2: float | None = None
    specified_area_m2: float | None = None
    declared_area_m2: float | None = None
    cadastral_cost_rub: float | None = None
    cost_index_rub_m2: float | None = None
    ownership_type: str | None = None
    land_record_type: str | None = None
    land_record_subtype: str | None = None
    reg_date: datetime | None = None
    has_borders: bool = False
    geometry: dict[str, Any] | None = None  # GeoJSON EPSG:4326
    zouit: list[ZoneHit] = field(default_factory=list)
    zouit_checked: bool = False
    territorial_zones: list[ZoneHit] = field(default_factory=list)
    territorial_zone_checked: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def best_area_m2(self) -> float | None:
        """Площадь для расчётов: уточнённая > общая > декларированная."""
        for v in (self.specified_area_m2, self.area_m2, self.declared_area_m2):
            if v:
                return v
        return None

    @property
    def area_sotka(self) -> float | None:
        a = self.best_area_m2
        return a / SOTKA_M2 if a else None

    @property
    def cadastral_cost_per_sotka(self) -> float | None:
        a = self.area_sotka
        if self.cadastral_cost_rub is None or not a:
            return None
        return self.cadastral_cost_rub / a

    @property
    def permitted_use_all(self) -> str:
        """Склеенный текст ВРИ из карточки и вкладки — для сопоставления с целью."""
        parts = [self.permitted_use or "", *self.permitted_use_tab]
        return " | ".join(p for p in parts if p)

    def field_sources(self) -> dict[str, Sourced[Any]]:
        """Прослеживаемость по полям для отчёта."""
        ts = self.fetched_at
        base = self.source
        out: dict[str, Sourced[Any]] = {
            "status": Sourced(self.status, base, ts),
            "category": Sourced(self.category, base, ts),
            "permitted_use": Sourced(self.permitted_use, base, ts),
            "area_m2": Sourced(self.best_area_m2, base, ts),
            "cadastral_cost_rub": Sourced(self.cadastral_cost_rub, base, ts),
            "ownership_type": Sourced(self.ownership_type, base, ts),
            "has_borders": Sourced(self.has_borders, base, ts),
        }
        if self.permitted_use_tab:
            out["permitted_use_tab"] = Sourced(
                self.permitted_use_tab, "НСПД, вкладка «Виды разрешенного использования»", ts
            )
        if self.zouit_checked:
            out["zouit"] = Sourced(
                [z.describe() for z in self.zouit], "НСПД, слои ЗОУИТ (пересечение контура)", ts
            )
        if self.territorial_zone_checked:
            out["territorial_zones"] = Sourced(
                [z.describe() for z in self.territorial_zones],
                "НСПД, слой «Территориальные зоны» (пересечение контура)",
                ts,
            )
        return out


@dataclass
class Flag:
    """Один риск-флаг: уровень, одно предложение понятным языком, источник."""

    rule_id: str
    level: Level
    message: str
    source: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class CheckResult:
    """Итог по одному кадастровому номеру."""

    listing: Listing
    parcel: Parcel
    flags: list[Flag]
    duplicates: list[Listing] = field(default_factory=list)
    score: float = 0.0

    @property
    def verdict(self) -> Level:
        if not self.flags:
            return Level.OK
        return max((f.level for f in self.flags), key=lambda lv: lv.rank)

    @property
    def stops(self) -> list[Flag]:
        return [f for f in self.flags if f.level is Level.STOP]

    @property
    def checks(self) -> list[Flag]:
        return [f for f in self.flags if f.level is Level.CHECK]

    @property
    def infos(self) -> list[Flag]:
        return [f for f in self.flags if f.level is Level.OK]

    @property
    def effective_area_m2(self) -> float | None:
        return self.parcel.best_area_m2 or self.listing.declared_area_m2

    @property
    def price_per_sotka(self) -> float | None:
        a = self.effective_area_m2
        if self.listing.price_rub is None or not a:
            return None
        return self.listing.price_rub / (a / SOTKA_M2)


@dataclass
class Request:
    """Запрос брокера: цель, бюджет, площадь."""

    purpose: Purpose = Purpose.ANY
    budget_rub: float | None = None
    min_area_m2: float | None = None
    max_area_m2: float | None = None
    region_hint: str | None = None


@dataclass
class BatchResult:
    request: Request
    results: list[CheckResult]
    started_at: datetime
    finished_at: datetime
    median_price_per_sotka: float | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        c = {lv.value: 0 for lv in Level}
        for r in self.results:
            c[r.verdict.value] += 1
        return c
