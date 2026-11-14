"""Движок правил: правило = функция контекста → флаги. Каталог — в catalog.py."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from landcheck.models import Flag, Listing, Parcel, Request


@dataclass
class RulesConfig:
    area_check_pct: float = 10.0  # расхождение площади «проверить»
    area_stop_pct: float = 25.0  # расхождение площади «стоп»
    price_vs_cadastral_low: float = 0.3  # цена < 30% кадастровой → проверить
    price_vs_cadastral_high: float = 4.0  # цена > 4× кадастровой → проверить
    price_vs_median_low: float = 0.5
    price_vs_median_high: float = 2.0
    median_min_samples: int = 5
    stale_days: float = 7.0


@dataclass
class BatchStats:
    median_price_per_sotka: float | None = None
    priced_count: int = 0


@dataclass
class RuleContext:
    listing: Listing
    parcel: Parcel
    request: Request
    stats: BatchStats = field(default_factory=BatchStats)
    config: RulesConfig = field(default_factory=RulesConfig)
    duplicates: list[Listing] = field(default_factory=list)
    fetch_errors: list[str] = field(default_factory=list)


RuleFn = Callable[[RuleContext], "list[Flag] | Flag | None"]


@dataclass(frozen=True)
class Rule:
    id: str
    title: str
    description: str
    fn: RuleFn

    def run(self, ctx: RuleContext) -> list[Flag]:
        out = self.fn(ctx)
        if out is None:
            return []
        if isinstance(out, Flag):
            return [out]
        return list(out)


RULES: list[Rule] = []


def rule(id: str, title: str, description: str) -> Callable[[RuleFn], RuleFn]:
    def deco(fn: RuleFn) -> RuleFn:
        RULES.append(Rule(id=id, title=title, description=description, fn=fn))
        return fn

    return deco


def evaluate(ctx: RuleContext) -> list[Flag]:
    # Регистрируем каталог лениво, чтобы избежать циклического импорта.
    from landcheck.rules import catalog  # noqa: F401

    flags: list[Flag] = []
    for r in RULES:
        flags.extend(r.run(ctx))
    return flags
