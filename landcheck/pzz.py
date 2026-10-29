"""Реестр ссылок на ПЗЗ и ориентиры стоимости подключения сетей (ТЗ v3, п. 4).

Данные — landcheck/data/pzz_registry.json и utilities.json: внутри дата проверки и признак
verified у каждой ссылки (адрес прочитан роботом или только известен). Муниципалитет
подбирается по подстроке адреса участка, регион — по первым двум цифрам кадастрового номера.
Ничего не выдумываем: нет записи — остаются федеральный реестр и поиск по муниципалитету.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import quote

from landcheck.region import locality_from_address

DATA_DIR = Path(__file__).resolve().parent / "data"


@lru_cache(maxsize=1)
def _registry() -> dict[str, Any]:
    return json.loads((DATA_DIR / "pzz_registry.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _utilities() -> dict[str, Any]:
    return json.loads((DATA_DIR / "utilities.json").read_text(encoding="utf-8"))


def region_code(cad_num: str | None) -> str | None:
    if not cad_num or ":" not in cad_num:
        return None
    head = cad_num.split(":", 1)[0].strip()
    return head.zfill(2) if head.isdigit() else None


def search_url(parcel: dict[str, Any]) -> str | None:
    where = locality_from_address(parcel.get("address")) or parcel.get("address")
    if not where:
        return None
    return "https://yandex.ru/search/?text=" + quote(
        f"правила землепользования и застройки карта градостроительного зонирования {where}"
    )


def links_for(parcel: dict[str, Any]) -> dict[str, Any]:
    """Ссылки на ПЗЗ для участка: муниципальные (если адрес совпал), региональные, федеральные."""
    reg = _registry()
    code = region_code(parcel.get("cad_num"))
    region = reg["regions"].get(code or "", {})
    address = (parcel.get("address") or "").lower()
    municipal: dict[str, Any] | None = None
    for m in region.get("municipalities", []):
        if any(k in address for k in m["match"]):
            municipal = m
            break
    return {
        "region": region.get("name"),
        "municipal": municipal,
        "regional": region.get("links", []),
        "federal": reg["federal"],
        "search_url": search_url(parcel),
        "checked": reg["checked"],
    }


def electricity_for(parcel: dict[str, Any]) -> dict[str, Any]:
    """Ориентир платы за присоединение до 15 кВт по региону участка + общие правила 2026."""
    u = _utilities()["electricity"]
    code = region_code(parcel.get("cad_num"))
    region = dict(u["regions"].get(code or "", {}))
    return {
        "region_code": code,
        "region": region or None,
        "preferential_rub_kw": u["preferential_rub_kw"],
        "preferential_note": u["preferential_note"],
        "corridor_rub_kw": u["corridor_rub_kw"],
        "note": u["note"],
        "sources": u["sources"],
        "checked": _utilities()["checked"],
    }


def gas_info() -> dict[str, Any]:
    return _utilities()["gas"]
