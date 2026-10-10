"""М3. Составитель объявления: черновик текста из фактов НСПД и заметок продавца, контроль
противоречий с кадастром, выгрузка в формат автозагрузки Avito (XML).

Теги Avito взяты по сторонним описаниям формата недвижимости (Category, OperationType,
ObjectType, LandArea, Price, Description, Address, Images). Перед первой загрузкой файл
нужно прогнать через проверку XML в кабинете автозагрузки Avito.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from xml.sax.saxutils import escape

from landcheck.models import SOTKA_M2, Parcel, Purpose
from landcheck.rules.purpose import category_class, vri_match

AVITO_OBJECT_TYPES = {
    "izhs": "Поселений (ИЖС)",
    "snt": "Сельхозназначения (СНТ, ДНП)",
    "industry": "Промназначения",
}


@dataclass
class Draft:
    cad_num: str
    title: str
    description: str
    object_type: str
    land_area_sotka: float | None
    price_rub: float | None
    address: str | None
    facts: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    zouit_notes: list[str] = field(default_factory=list)


def _guess_object_type(parcel: Parcel) -> str:
    cls = category_class(parcel.category)
    vri = parcel.permitted_use_all
    if cls == "industry":
        return AVITO_OBJECT_TYPES["industry"]
    if cls == "agri" or vri_match(Purpose.SNT, vri) == "match":
        return AVITO_OBJECT_TYPES["snt"]
    return AVITO_OBJECT_TYPES["izhs"]


def _short_locality(address: str | None) -> str | None:
    if not address:
        return None
    # «обл. Московская, р-н Истринский, д. Бужарово, …» → первые 3 части
    parts = [p.strip() for p in re.split(r"[,;]", address) if p.strip()]
    return ", ".join(parts[:3]) if parts else address


def _fmt_sotka(m2: float | None) -> str | None:
    if not m2:
        return None
    v = m2 / SOTKA_M2
    return f"{v:.0f}" if abs(v - round(v)) < 0.05 else f"{v:.1f}"


def contradictions(parcel: Parcel, notes: str, price_rub: float | None) -> list[str]:
    """Утверждения в заметках продавца, которые расходятся с кадастром."""
    out: list[str] = []
    t = (notes or "").lower()
    cls = category_class(parcel.category)
    if ("ижс" in t or "под дом" in t or "построить дом" in t) and cls == "agri":
        out.append("в заметках ИЖС / дом, а по НСПД земля сельхозназначения — писать «ИЖС» нельзя")
    if (
        ("ижс" in t)
        and parcel.permitted_use_all
        and vri_match(Purpose.IZHS, parcel.permitted_use_all) == "conflict"
    ):
        out.append(
            f"в заметках ИЖС, а ВРИ по НСПД «{parcel.permitted_use}» — указать фактический ВРИ"
        )
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*сот", t)
    if m and parcel.best_area_m2:
        claimed = float(m.group(1).replace(",", "."))
        actual = parcel.best_area_m2 / SOTKA_M2
        if actual and abs(claimed - actual) / actual > 0.1:
            out.append(
                f"в заметках {claimed:g} сот., по ЕГРН {actual:.1f} сот. — в объявление идёт площадь ЕГРН"
            )
    if ("межеван" in t or "границы установлены" in t) and not parcel.has_borders:
        out.append("в заметках межевание сделано, а в ЕГРН границы не установлены")
    if "без обременений" in t and parcel.zouit:
        out.append(
            f"в заметках «без обременений», а участок пересекает {len(parcel.zouit)} ЗОУИТ — не обещать"
        )
    if price_rub and parcel.cadastral_cost_rub and price_rub < 0.3 * parcel.cadastral_cost_rub:
        out.append(
            "цена ниже 30% кадастровой стоимости — покупатели заподозрят подвох, проверьте цифру"
        )
    return out


def build_draft(
    parcel: Parcel,
    *,
    notes: str = "",
    price_rub: float | None = None,
    object_type: str | None = None,
) -> Draft:
    area_s = _fmt_sotka(parcel.best_area_m2)
    locality = _short_locality(parcel.address)
    obj = object_type or _guess_object_type(parcel)
    vri = parcel.permitted_use or (
        parcel.permitted_use_tab[0] if parcel.permitted_use_tab else None
    )

    facts: list[str] = []
    if area_s:
        facts.append(f"Площадь по ЕГРН {area_s} сот.")
    if parcel.category:
        facts.append(f"Категория: {parcel.category.lower()}")
    if vri:
        facts.append(f"ВРИ: {vri}")
    facts.append(
        "Границы установлены, участок отмежёван"
        if parcel.has_borders
        else "Границы в ЕГРН не установлены"
    )
    if parcel.cadastral_cost_rub:
        facts.append(f"Кадастровая стоимость {parcel.cadastral_cost_rub:,.0f} ₽".replace(",", " "))
    if parcel.territorial_zones:
        facts.append(
            "Территориальная зона по ПЗЗ: "
            + "; ".join(z.describe(max_len=60) for z in parcel.territorial_zones[:2])
        )

    zouit_notes = [z.describe(max_len=80) for z in parcel.zouit]

    title_bits = ["Участок"]
    if area_s:
        title_bits.append(f"{area_s} сот.")
    if obj == AVITO_OBJECT_TYPES["izhs"]:
        title_bits.append(
            "ИЖС" if vri and vri_match(Purpose.IZHS, vri) == "match" else "в населённом пункте"
        )
    elif obj == AVITO_OBJECT_TYPES["snt"]:
        title_bits.append("садоводство")
    else:
        title_bits.append("промназначения")
    if locality:
        title_bits.append(f"— {locality}")
    title = " ".join(title_bits)

    paragraphs: list[str] = []
    lead = f"Продаётся земельный участок {area_s + ' сот. ' if area_s else ''}"
    if locality:
        lead += f"({locality}). "
    lead += f"Кадастровый номер {parcel.cad_num}."
    paragraphs.append(lead.strip())
    paragraphs.append("По данным ЕГРН: " + "; ".join(facts) + ".")
    if notes.strip():
        paragraphs.append(notes.strip())
    if zouit_notes:
        paragraphs.append(
            "Ограничения из публичных данных (ЗОУИТ): "
            + "; ".join(zouit_notes)
            + ". Уточняйте перед строительством."
        )
    paragraphs.append(
        "Проверка по открытым данным НСПД на "
        + datetime.now(UTC).strftime("%d.%m.%Y")
        + ". Документы покажем при осмотре."
    )
    description = "\n\n".join(paragraphs)

    warnings = contradictions(parcel, notes, price_rub)
    if not parcel.has_borders:
        warnings.append(
            "без межевания сделку не зарегистрируют (487-ФЗ) — сначала межевание, потом продажа"
        )
    return Draft(
        cad_num=parcel.cad_num,
        title=title[:50] if len(title) > 50 else title,
        description=description,
        object_type=obj,
        land_area_sotka=round(parcel.best_area_m2 / SOTKA_M2, 2) if parcel.best_area_m2 else None,
        price_rub=price_rub,
        address=parcel.address,
        facts=facts,
        warnings=warnings,
        zouit_notes=zouit_notes,
    )


def avito_xml(
    draft: Draft,
    *,
    ad_id: str,
    phone: str | None = None,
    manager: str | None = None,
    image_urls: list[str] | None = None,
) -> str:
    def tag(name: str, value: Any) -> str:
        if value is None or value == "":
            return ""
        return f"    <{name}>{escape(str(value))}</{name}>\n"

    images = ""
    if image_urls:
        images = (
            "    <Images>\n"
            + "".join(
                f'      <Image url="{escape(u, {chr(34): "&quot;"})}"/>\n' for u in image_urls
            )
            + "    </Images>\n"
        )
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Ads formatVersion="3" target="Avito.ru">\n'
        "  <Ad>\n"
        + tag("Id", ad_id)
        + tag("Category", "Земельные участки")
        + tag("OperationType", "Продам")
        + tag("ObjectType", draft.object_type)
        + tag("Address", draft.address)
        + tag("LandArea", f"{draft.land_area_sotka:g}" if draft.land_area_sotka else None)
        + tag("Price", f"{draft.price_rub:.0f}" if draft.price_rub else None)
        + tag("CadastralNumber", draft.cad_num)
        + tag("Title", draft.title)
        + tag("Description", draft.description)
        + tag("ContactPhone", phone)
        + tag("ManagerName", manager)
        + images
        + "  </Ad>\n"
        "</Ads>\n"
    )
    return body
