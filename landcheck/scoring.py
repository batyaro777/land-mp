"""Оценка соответствия запросу и сортировка выдачи."""

from __future__ import annotations

from landcheck.models import CheckResult, Level, Request


def score(result: CheckResult, request: Request) -> float:
    s = 100.0
    for f in result.flags:
        if f.level is Level.STOP:
            s -= 50
        elif f.level is Level.CHECK:
            s -= 10
        elif f.rule_id == "R14":  # выше бюджета
            s -= 30
        elif f.rule_id == "R15":  # вне диапазона площади
            s -= 15
    return max(s, -100.0)


def sort_key(result: CheckResult):
    pps = result.price_per_sotka
    return (
        result.verdict.rank,
        -result.score,
        pps is None,
        pps if pps is not None else 0.0,
        result.listing.cad_num,
    )
