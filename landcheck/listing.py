"""М3. Составитель объявления: черновик текста из фактов НСПД и заметок продавца, контроль
противоречий с кадастром, выгрузка в формат автозагрузки Avito (XML).

Теги Avito взяты по сторонним описаниям формата недвижимости (Category, OperationType,
ObjectType, LandArea, Price, Description, Address, Images). Перед первой загрузкой файл
нужно прогнать через проверку XML в кабинете автозагрузки Avito.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from xml.sax.saxutils import escape

from landcheck.models import SOTKA_M2, Parcel, Purpose
from landcheck.rules.purpose import category_class, vri_match

AVITO_OBJECT_TYPES = {
    "izhs": "Поселений (ИЖС)",
    "snt": "Сельхозназначения (СНТ, ДНП)",
    "industry": "Промназначения",
}


_UNSET_RE = re.compile(r"не установлен", re.IGNORECASE)
# Служебные фразы, которые модель пишет покупателю вместо фактов (прод 20.10)
SERVICE_RE = re.compile(
    r"(категория не установлена|не указан[аоы]? в |в предоставленных данных|исходных данных|"
    r"не предоставил|дополнительн\w* сведен|сведений нет|нет данных о|отсутствует в данных)",
    re.IGNORECASE,
)


def normalize_category(category: str | None) -> str | None:
    """НСПД присылает «Категория не установлена» строкой, а не пустым полем (прод 20.10) —
    для всех проверок это отсутствие категории."""
    if not category or _UNSET_RE.search(category):
        return None
    return category


@dataclass
class ParcelFacts:
    """Минимум фактов участка для повторной проверки текста после правок (хранится в БД)."""

    cad_num: str
    category: str | None
    permitted_use: str | None
    permitted_use_all: str
    best_area_m2: float | None
    has_borders: bool
    zouit_count: int
    cadastral_cost_rub: float | None
    fetched_at: str | None = None

    @classmethod
    def from_parcel(cls, p: Parcel) -> ParcelFacts:
        return cls(
            cad_num=p.cad_num,
            category=normalize_category(p.category),
            permitted_use=p.permitted_use,
            permitted_use_all=p.permitted_use_all,
            best_area_m2=p.best_area_m2,
            has_borders=p.has_borders,
            zouit_count=len(p.zouit),
            cadastral_cost_rub=p.cadastral_cost_rub,
            fetched_at=p.fetched_at.isoformat(),
        )

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ParcelFacts:
        obj = cls(**{k: d.get(k) for k in cls.__dataclass_fields__})  # type: ignore[arg-type]
        obj.category = normalize_category(obj.category)
        return obj

    def to_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


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
    category = normalize_category(parcel.category)
    cls = category_class(category)
    vri = parcel.permitted_use_all
    if cls == "industry":
        return AVITO_OBJECT_TYPES["industry"]
    if cls == "agri" or vri_match(Purpose.SNT, vri) == "match":
        return AVITO_OBJECT_TYPES["snt"]
    if not category and not vri:
        return ""  # ни категории, ни ВРИ в ЕГРН — рубрику площадки не определить
    return AVITO_OBJECT_TYPES["izhs"]


def _short_locality(address: str | None) -> str | None:
    if not address:
        return None
    # «обл. Московская, р-н Истринский, д. Бужарово, …» → первые 3 части
    parts = [p.strip() for p in re.split(r"[,;]", address) if p.strip()]
    return ", ".join(parts[:3]) if parts else address


MIN_AREA_M2 = 50  # меньше полусотки — ошибка данных ЕГРН (1 м² встречается), не площадь участка


def _fmt_sotka(m2: float | None) -> str | None:
    if not m2:
        return None
    v = m2 / SOTKA_M2
    if v < 1:
        return f"{v:.2f}".rstrip("0").rstrip(".")
    return f"{v:.0f}" if abs(v - round(v)) < 0.05 else f"{v:.1f}"


def strip_facts(text: str, facts: list[str] | None, zouit_notes: list[str] | None) -> str:
    """Убрать из текста дословные факты ЕГРН и ЗОУИТ: название терзоны «Зона застройки ИЖС»
    или ВРИ из НСПД — факт, а не обещание продавца, и в контроле противоречий участвовать
    не должно (на фикстуре Ж-1 такой факт блокировал публикацию честного объявления)."""
    out = text or ""
    for chunk in [*(facts or []), *(zouit_notes or [])]:
        if chunk:
            out = out.replace(chunk, " ")
    return out


def contradictions(f: ParcelFacts, text: str, price_rub: float | None) -> list[str]:
    """Утверждения в тексте (заметки продавца или готовое объявление), которые расходятся с кадастром."""
    out: list[str] = []
    t = (text or "").lower()
    cls = category_class(f.category)
    if ("ижс" in t or "под дом" in t or "построить дом" in t) and cls == "agri":
        out.append("в тексте ИЖС / дом, а по НСПД земля сельхозназначения — писать «ИЖС» нельзя")
    if (
        "ижс" in t
        and f.permitted_use_all
        and vri_match(Purpose.IZHS, f.permitted_use_all) == "conflict"
    ):
        out.append(f"в тексте ИЖС, а ВРИ по НСПД «{f.permitted_use}» — указать фактический ВРИ")
    if "ижс" in t and not f.permitted_use_all and f.category is None:
        out.append("в тексте ИЖС, а категория и ВРИ в ЕГРН не установлены — назначение не обещать")
    if f.best_area_m2 and f.best_area_m2 < MIN_AREA_M2:
        m = re.search(r"(\d+(?:[.,]\d+)?)\s*(сот|кв\.?\s*м|м²|м2)", t)
        if m and float(m.group(1).replace(",", ".")):
            out.append(
                f"в тексте площадь «{m.group(0)}», а по ЕГРН она не определена (ошибка данных) — "
                "уберите площадь из текста"
            )
    if f.best_area_m2 and f.best_area_m2 >= MIN_AREA_M2:
        actual = f.best_area_m2 / SOTKA_M2
        for m in re.finditer(r"(\d+(?:[.,]\d+)?)\s*сот", t):
            claimed = float(m.group(1).replace(",", "."))
            if claimed and actual and abs(claimed - actual) / actual > 0.1:
                out.append(
                    f"в тексте {claimed:g} сот., по ЕГРН {actual:.1f} сот. — в объявление идёт площадь ЕГРН"
                )
                break
    m_svc = SERVICE_RE.search(t)
    if m_svc:
        out.append(
            f"в тексте служебная фраза «{m_svc.group(0).strip()}…» — уберите, покупателю она не нужна"
        )
    if ("межеван" in t or "границы установлены" in t) and not f.has_borders:
        out.append("в тексте межевание сделано, а в ЕГРН границы не установлены")
    if "без обременений" in t and f.zouit_count:
        out.append(
            f"в тексте «без обременений», а участок пересекает {f.zouit_count} ЗОУИТ — не обещать"
        )
    if price_rub and f.cadastral_cost_rub and price_rub < 0.3 * f.cadastral_cost_rub:
        out.append(
            "цена ниже 30% кадастровой стоимости — покупатели заподозрят подвох, проверьте цифру"
        )
    return out


def static_warnings(f: ParcelFacts) -> list[str]:
    """Предупреждения по самому участку, не зависящие от текста."""
    out: list[str] = []
    if not f.has_borders:
        out.append(
            "без межевания сделку не зарегистрируют (487-ФЗ) — сначала межевание, потом продажа"
        )
    if f.best_area_m2 and f.best_area_m2 < MIN_AREA_M2:
        out.append(
            f"площадь по ЕГРН {f.best_area_m2:g} м² — похоже на ошибку данных или долю; "
            "уточнить по выписке, в объявлении площадь не обещать"
        )
    if not f.category and not f.permitted_use_all:
        out.append(
            "категория земель и ВРИ в ЕГРН не установлены — рубрику площадки не определить, "
            "назначение в объявлении не указывать; запросить выписку ЕГРН"
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
    tiny = bool(parcel.best_area_m2) and parcel.best_area_m2 < MIN_AREA_M2
    if tiny:
        facts.append(
            f"Площадь по ЕГРН не определена: в карточке {parcel.best_area_m2:g} м², "
            "похоже на ошибку данных — уточнить по выписке"
        )
        area_s = None
    elif area_s:
        facts.append(f"Площадь по ЕГРН {area_s} сот.")
    category = normalize_category(parcel.category)
    if category:
        facts.append(f"Категория: {category.lower()}")
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

    # Дубли убираем по реестровому номеру или полному описанию, не по обрезанной строке:
    # иначе «Граница III зоны округа…» и «Граница округа…» склеивались (прод 20.10).
    uniq: dict[str, Any] = {}
    for z in parcel.zouit:
        uniq.setdefault(z.reg_number or z.describe(max_len=10_000), z)
    zouit_notes = [z.listing_note(max_len=120) for z in uniq.values()]

    title_bits = ["Участок"]
    if area_s:
        title_bits.append(f"{area_s} сот.")
    if obj == AVITO_OBJECT_TYPES["izhs"]:
        title_bits.append(
            "ИЖС" if vri and vri_match(Purpose.IZHS, vri) == "match" else "в населённом пункте"
        )
    elif obj == AVITO_OBJECT_TYPES["snt"]:
        title_bits.append("садоводство")
    elif obj == AVITO_OBJECT_TYPES["industry"]:
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
        "Данные НСПД от "
        + parcel.fetched_at.strftime("%d.%m.%Y")
        + ". Документы покажем при осмотре."
    )
    description = "\n\n".join(paragraphs)

    pf = ParcelFacts.from_parcel(parcel)
    warnings = contradictions(pf, strip_facts(description, facts, zouit_notes), price_rub)
    warnings += static_warnings(pf)
    return Draft(
        cad_num=parcel.cad_num,
        title=title[:50] if len(title) > 50 else title,
        description=description,
        object_type=obj,
        land_area_sotka=(
            round(parcel.best_area_m2 / SOTKA_M2, 2) if parcel.best_area_m2 and not tiny else None
        ),
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
