"""Автосверка выписки ЕГРН (К2, антифейк М5): текст выписки → номер, площадь, правообладатель,
вид права, дата, обременения → сравнение с объявлением и продавцом.

ФИО из документа в базу не попадает: наружу уходит только статус сверки и маска «И***в И. И.».
Решение остаётся за модератором: сверка подсвечивает, а не блокирует.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from landcheck.inputs import CAD_RE

STALE_DAYS = 30
EXTRACT_TZ_OFFSET = timedelta(hours=3)  # время формирования выписки — московское

_DATE_RE = re.compile(r"\b(\d{2})\.(\d{2})\.(\d{4})\b")
_EXTRACT_RE = re.compile(
    r"выписк\w*\s+из\s+единого\s+государственного\s+реестра|ЕГРН|единого\s+государственного"
    r"\s+реестра\s+недвижимости|сведения\s+об\s+основных\s+характеристиках",
    re.I,
)
_COMPANY_RE = re.compile(
    r"\b(ООО|АО|ПАО|ЗАО|ОАО|ИП|МУП|ГУП|ФГУП|администрац|муниципальн|общество|товариществ|"
    r"кооператив|учреждени|ИНН|ОГРН|Российская Федерация|субъект)",
    re.I,
)
_STOP_RE = (
    r"(?=\s*(?:\d+\.\s*)?(?:\b(?:Вид,|Вид\s+права|Документ|Документы-основания|Ограничени|Сведения|"
    r"Заявлен|Правопритяз|Договор|Кадастров|Площадь|Адрес|Местоположени)|$))"
)
_HOLDER_RE = re.compile(
    r"Правообладател\w*(?:\s*\(правообладатели\))?\s*:\s*(.+?)" + _STOP_RE, re.I | re.S
)
_RIGHT_RE = re.compile(
    r"Вид,?\s*номер\s*и\s*дата\s*(?:государственной\s*)?регистрации\s*права\s*:\s*(.+?)" + _STOP_RE,
    re.I | re.S,
)
_RIGHT_KIND_RE = re.compile(
    r"(собственност\w*|аренд\w*|пожизненн\w+\s+наследуем\w+\s+владени\w*|постоянн\w+\s*"
    r"\(?бессрочн\w*\)?\s*пользовани\w*|безвозмездн\w+\s+пользовани\w*|сервитут|ипотек\w*|"
    r"хозяйственн\w+\s+ведени\w*|оперативн\w+\s+управлени\w*)",
    re.I,
)
_AREA_RE = re.compile(r"Площадь[^\d\n]{0,40}?(\d[\d\s ]*(?:[.,]\d+)?)\s*(?:\+/-|±)?", re.I)
_ADDRESS_RE = re.compile(
    r"(?:Местоположение|Адрес(?:\s*\(местоположение\))?)\s*(?:объекта)?\s*:\s*(.+?)"
    r"(?=\s*(?:\b(?:Площадь|Кадастров|Категори|Вид|Правооблад|Назначени|Статус)|\n|$))",
    re.I | re.S,
)
_ENC_RE = re.compile(
    r"Ограничени\w*\s+прав\s+и\s+обременени\w*[^:]{0,60}:\s*(.+?)"
    r"(?=\s*(?:Сведения|Договор|Заявлен|Правопритяз|Документ|Получатель|Полное наименование|$))",
    re.I | re.S,
)
_ENC_KIND_RE = re.compile(
    r"(ипотек\w*|арест\w*|запре\w+|аренд\w*|сервитут\w*|залог\w*|доверительн\w+\s+управлени\w*|"
    r"рент\w*|найм\w*)",
    re.I,
)
_ISSUED_RE = re.compile(
    r"(?:по\s+состоянию\s+на|дата\s+формирования|сформирован\w*|выписка\s+от|дата\s+выдачи|"
    r"выдана|дата\s+и\s+время\s+формирования|на\s+дату)\D{0,40}?(\d{2}\.\d{2}\.\d{4})",
    re.I,
)
_NONE_RE = re.compile(r"не\s+зарегистрирован|отсутству|данные\s+отсутствуют|нет", re.I)


@dataclass
class Extract:
    """Что прочитали из выписки. holders — как в документе; наружу их не отдаём."""

    is_extract: bool = False
    cad_num: str | None = None
    area_m2: float | None = None
    address: str | None = None
    holders: list[str] = field(default_factory=list)
    right_kind: str | None = None
    right_date: str | None = None
    issued: str | None = None
    encumbrances: list[str] = field(default_factory=list)
    encumbrances_known: bool = False  # раздел обременений найден в тексте
    category: str | None = None
    permitted_use: str | None = None
    right_number: str | None = None
    issued_at: str | None = None  # ISO 8601, когда известно время формирования (XML)
    source: str = "text"  # text (PDF/скан) | xml

    @property
    def filled(self) -> int:
        return sum(
            1 for v in (self.cad_num, self.area_m2, self.holders, self.right_kind, self.issued) if v
        )


def parse_extract(text: str) -> Extract:
    """Регулярки по печатной форме выписки (Госуслуги/Росреестр, формы 2019–2026)."""
    ex = Extract()
    if not text or not text.strip():
        return ex
    t = text.replace("\xa0", " ").replace(" ", " ")
    t = re.sub(r"[ \t]+", " ", t)
    ex.is_extract = bool(_EXTRACT_RE.search(t))
    m = re.search(r"Кадастровый\s+номер\s*:?\s*(" + CAD_RE.pattern + ")", t, re.I)
    cad = m.group(1) if m else (CAD_RE.search(t).group(0) if CAD_RE.search(t) else None)
    ex.cad_num = re.sub(r"\s+", "", cad) if cad else None
    m = _AREA_RE.search(t)
    if m:
        try:
            ex.area_m2 = float(m.group(1).replace(" ", "").replace(",", "."))
        except ValueError:
            ex.area_m2 = None
    m = _ADDRESS_RE.search(t)
    if m:
        ex.address = re.sub(r"\s+", " ", m.group(1)).strip(" ,;")[:300] or None
    m = _HOLDER_RE.search(t)
    if m:
        ex.holders = _split_holders(m.group(1))
    m = _RIGHT_RE.search(t)
    if m:
        chunk = m.group(1)
        km = _RIGHT_KIND_RE.search(chunk)
        ex.right_kind = km.group(1).lower() if km else None
        dates = _DATE_RE.findall(chunk)
        if dates:
            d, mo, y = dates[-1]
            ex.right_date = f"{d}.{mo}.{y}"
    else:
        km = re.search(r"Вид\s+права\s*:\s*([^\n;]+)", t, re.I)
        if km:
            kk = _RIGHT_KIND_RE.search(km.group(1))
            ex.right_kind = kk.group(1).lower() if kk else None
    m = _ISSUED_RE.search(t)
    if m:
        ex.issued = m.group(1)
    m = _ENC_RE.search(t)
    if m:
        ex.encumbrances_known = True
        chunk = m.group(1)[:600]
        if not _NONE_RE.search(chunk[:80]):
            ex.encumbrances = sorted({k.lower() for k in _ENC_KIND_RE.findall(chunk)})
            if not ex.encumbrances and chunk.strip():
                ex.encumbrances = ["есть запись"]
    return ex


def _split_holders(chunk: str) -> list[str]:
    chunk = re.sub(r"\s+", " ", chunk).strip()
    parts = re.split(r"\s*\d+\.\d+\.?\s*", chunk)
    out = []
    for p in parts:
        p = p.strip(" ,;:")
        p = re.sub(r",?\s*(?:\d{2}\.\d{2}\.\d{4}\s*(?:г\.?\s*р\.?|года\s+рождения)?).*$", "", p)
        p = re.sub(
            r",?\s*(?:паспорт|СНИЛС|ИНН\s*\d|дата рождения|место рождения).*$", "", p, flags=re.I
        )
        if p:
            out.append(p[:200])
    return out or ([chunk[:200]] if chunk else [])


# --- сравнение --------------------------------------------------------------------------------
@dataclass
class CheckItem:
    key: str
    label: str
    status: str  # ok|mismatch|warn|unknown
    note: str = ""


@dataclass
class CheckResult:
    verdict: str  # ok|mismatch|unknown
    label: str
    items: list[CheckItem] = field(default_factory=list)
    checked_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "label": self.label,
            "checked_at": self.checked_at,
            "items": [asdict(i) for i in self.items],
        }


VERDICT_LABELS = {
    "ok": "совпадает",
    "mismatch": "расхождение",
    "unknown": "не сверено",
}


def normalize_name(s: str) -> list[str]:
    s = s.lower().replace("ё", "е")
    s = re.sub(r"[^а-яa-z\s.-]", " ", s)
    return [t.strip(".-") for t in re.split(r"[\s.]+", s) if t.strip(".-")]


def mask_name(s: str) -> str:
    """«Иванов Иван Иванович» → «И***в И. И.» — модератор видит документ сам, в базе ФИО нет."""
    toks = [t for t in re.split(r"\s+", s.strip()) if t]
    if not toks:
        return ""
    first = toks[0]
    head = first[0] + "***" + first[-1] if len(first) > 2 else first[0] + "***"
    return " ".join([head, *[t[0] + "." for t in toks[1:3]]])


def name_match(seller: str, holder: str) -> str:
    """ok — совпадает (полностью или фамилия+инициалы); mismatch — другой человек; unknown —
    юрлицо или имя продавца не разобрать."""
    if _COMPANY_RE.search(holder):
        return "unknown"
    s, h = normalize_name(seller), normalize_name(holder)
    if not s or not h:
        return "unknown"
    s_full = [t for t in s if len(t) > 1]
    h_full = [t for t in h if len(t) > 1]
    common = set(s_full) & set(h_full)
    if not common:
        return "mismatch" if s_full else "unknown"
    if len(s_full) == 1 and h_full and s_full[0] != h_full[0]:
        return "unknown"  # у продавца одно слово, и это не фамилия (в выписке она первая)
    s_rest = [t for t in s if t not in common]
    h_rest = [t for t in h if t not in common]
    if not s_rest or not h_rest:
        return "ok"
    s_rest_full = [t for t in s_rest if len(t) > 1]
    h_rest_full = [t for t in h_rest if len(t) > 1]
    if s_rest_full and h_rest_full:
        return "ok" if set(s_rest_full) & set(h_rest_full) else "mismatch"
    return "ok" if {t[0] for t in s_rest} & {t[0] for t in h_rest} else "mismatch"


def compare(
    ex: Extract,
    *,
    cad_num: str,
    area_m2: float | None = None,
    seller_name: str = "",
    proof_kind: str = "owner",
    today: date | None = None,
    signature: dict[str, Any] | None = None,
    created_at: datetime | None = None,
    category: str | None = None,
    permitted_use: str | None = None,
) -> CheckResult:
    """Сверка выписки с объявлением (НСПД — первоисточник), продавцом, подписью и свежестью.

    signature — результат `landcheck.sig.verify_detached(...).to_dict()`; created_at — когда
    создано объявление: выписка должна быть сформирована позже (заказана по нашему запросу).
    """
    today = today or datetime.now(UTC).date()
    items: list[CheckItem] = []
    if not ex.is_extract and ex.filled < 2:
        return CheckResult(
            verdict="unknown",
            label="Документ не похож на выписку ЕГРН: сверка не выполнена, проверьте вручную",
            checked_at=today.isoformat(),
        )
    # 0. подпись: подлинность файла
    items.append(_signature_item(signature))
    # 1. кадастровый номер
    if not ex.cad_num:
        items.append(CheckItem("cad", "Кадастровый номер", "unknown", "в документе не найден"))
    elif ex.cad_num == cad_num:
        items.append(CheckItem("cad", "Кадастровый номер", "ok", cad_num))
    else:
        items.append(
            CheckItem(
                "cad",
                "Кадастровый номер",
                "mismatch",
                f"в документе {ex.cad_num}, в объявлении {cad_num}",
            )
        )
    # 2. площадь
    if ex.area_m2 and area_m2:
        diff = abs(ex.area_m2 - area_m2) / max(area_m2, 1)
        st = "ok" if diff <= 0.03 else "mismatch"
        items.append(
            CheckItem(
                "area", "Площадь", st, f"в документе {ex.area_m2:g} м², по ЕГРН {area_m2:g} м²"
            )
        )
    else:
        items.append(CheckItem("area", "Площадь", "unknown", "не нашли в документе"))
    # 3. правообладатель
    if not ex.holders:
        items.append(CheckItem("holder", "Правообладатель", "unknown", "в документе не найден"))
    else:
        masked = ", ".join(mask_name(h) for h in ex.holders[:3])
        if proof_kind == "agent":
            items.append(
                CheckItem(
                    "holder",
                    "Правообладатель",
                    "unknown",
                    f"{masked}; размещает представитель — сверить с договором",
                )
            )
        else:
            statuses = [name_match(seller_name, h) for h in ex.holders]
            if "ok" in statuses:
                st, note = "ok", f"{masked} — совпадает с именем продавца"
            elif all(s == "unknown" for s in statuses):
                st, note = "unknown", f"{masked} — юрлицо или имя не разобрать, проверить вручную"
            else:
                st, note = "mismatch", f"{masked} — не совпадает с именем продавца «{seller_name}»"
            items.append(CheckItem("holder", "Правообладатель", st, note))
    # 4. вид права
    rk = ex.right_kind or ""
    if not rk:
        items.append(CheckItem("right", "Вид права", "unknown", "не найден"))
    elif rk.startswith("собственност"):
        note = "собственность" + (f" от {ex.right_date}" if ex.right_date else "")
        items.append(CheckItem("right", "Вид права", "ok", note))
    else:
        items.append(
            CheckItem("right", "Вид права", "warn", f"{rk}: продавать может только собственник")
        )
    # 5. дата выписки
    if ex.issued:
        try:
            d = datetime.strptime(ex.issued, "%d.%m.%Y").date()
            age = (today - d).days
            if age < 0:
                items.append(
                    CheckItem("date", "Дата выписки", "warn", f"{ex.issued}: дата в будущем")
                )
            elif age > STALE_DAYS:
                items.append(
                    CheckItem(
                        "date",
                        "Дата выписки",
                        "warn",
                        f"{ex.issued}: выписке {age} дн., свежая — до {STALE_DAYS}",
                    )
                )
            else:
                items.append(CheckItem("date", "Дата выписки", "ok", f"{ex.issued}, {age} дн."))
        except ValueError:
            items.append(CheckItem("date", "Дата выписки", "unknown", ex.issued))
    else:
        items.append(CheckItem("date", "Дата выписки", "unknown", "не найдена"))
    # 6. обременения
    if not ex.encumbrances_known:
        items.append(CheckItem("enc", "Обременения", "unknown", "раздел не найден"))
    elif ex.encumbrances:
        items.append(CheckItem("enc", "Обременения", "warn", ", ".join(ex.encumbrances)))
    else:
        items.append(CheckItem("enc", "Обременения", "ok", "не зарегистрированы"))
    # 7. свежесть: выписка заказана после создания объявления
    items.append(_freshness_item(ex, created_at))
    # 8. категория и ВРИ против НСПД (есть только в XML и в полных выписках)
    items.extend(_land_items(ex, category, permitted_use))

    by = {i.key: i.status for i in items}
    warns = [i.label.lower() for i in items if i.status == "warn"]
    signed = by.get("sig") == "ok"
    if any(i.status == "mismatch" for i in items):
        bad = [i.label.lower() for i in items if i.status == "mismatch"]
        verdict, label = "mismatch", "Расхождение: " + ", ".join(bad)
    elif by.get("cad") == "ok" and by.get("holder") == "ok":
        verdict, label = "ok", "Выписка совпадает с объявлением и продавцом"
        if signed:
            label += ", подпись Росреестра проверена"
    elif by.get("cad") == "ok" and proof_kind == "agent":
        verdict, label = "ok", "Номер совпадает; право собственника — по договору представителя"
    else:
        unk = [
            i.label.lower() for i in items if i.status == "unknown" and i.key in ("cad", "holder")
        ]
        verdict, label = (
            "unknown",
            "Сверка не завершена: " + ", ".join(unk or ["проверьте документ"]),
        )
    if warns:
        label += "; внимание: " + ", ".join(warns)
    return CheckResult(verdict=verdict, label=label, items=items, checked_at=today.isoformat())


def _signature_item(signature: dict[str, Any] | None) -> CheckItem:
    if not signature:
        return CheckItem(
            "sig", "Подпись", "unknown", "файл .sig не приложен: подлинность не проверена"
        )
    st = str(signature.get("status") or "")
    signer = str(signature.get("signer") or "").strip()
    who = f" — {signer}" if signer else ""
    if st == "ok":
        return CheckItem("sig", "Подпись", "ok", "верна, цепочка до корня проверена" + who)
    if st == "chain_unverified":
        return CheckItem("sig", "Подпись", "warn", "верна, цепочка до корня не проверена" + who)
    if st == "bad":
        return CheckItem(
            "sig", "Подпись", "mismatch", "не сходится: файл изменён или подпись от другого файла"
        )
    detail = str(signature.get("detail") or "")
    return CheckItem(
        "sig", "Подпись", "unknown", "проверка недоступна" + (f": {detail}" if detail else "")
    )


def _freshness_item(ex: Extract, created_at: datetime | None) -> CheckItem:
    """Выписка должна быть сформирована после создания объявления (заказана по нашему запросу).

    Время в выписке Росреестр пишет без часового пояса; считаем его московским (UTC+3) —
    это строже, чем принять за UTC, и закрывает окно в 3 часа (замечание Клавдия, прод 23.10).
    """
    if created_at is None:
        return CheckItem("fresh", "Свежесть", "unknown", "дата объявления неизвестна")
    if not ex.issued:
        return CheckItem("fresh", "Свежесть", "unknown", "дата формирования не найдена")
    created = created_at.astimezone(UTC).replace(tzinfo=None) if created_at.tzinfo else created_at
    issued: datetime | None = None
    if ex.issued_at:
        try:
            issued = datetime.fromisoformat(ex.issued_at)
        except ValueError:
            issued = None
    if issued is None:
        try:
            issued = datetime.strptime(ex.issued, "%d.%m.%Y").replace(hour=23, minute=59)
        except ValueError:
            return CheckItem("fresh", "Свежесть", "unknown", ex.issued)
    if issued.tzinfo:
        issued = issued.astimezone(UTC).replace(tzinfo=None)
    else:
        issued = issued - EXTRACT_TZ_OFFSET  # московское → UTC
    when = ex.issued + (
        f" {ex.issued_at[11:16]} МСК" if ex.issued_at and len(ex.issued_at) >= 16 else ""
    )
    if issued >= created:
        return CheckItem(
            "fresh", "Свежесть", "ok", f"сформирована {when}, после создания объявления"
        )
    return CheckItem(
        "fresh",
        "Свежесть",
        "warn",
        f"сформирована {when}, до создания объявления {created:%d.%m.%Y %H:%M} UTC: "
        "закажите новую на Госуслугах (бесплатно, 1 минута)",
    )


def _land_items(ex: Extract, category: str | None, permitted_use: str | None) -> list[CheckItem]:
    from landcheck.listing import normalize_category

    out: list[CheckItem] = []
    if ex.category and category:
        a, b = normalize_category(ex.category), normalize_category(category)
        if a and b:
            st = "ok" if _fold(a) == _fold(b) else "mismatch"
            out.append(
                CheckItem(
                    "category",
                    "Категория",
                    st,
                    f"в документе «{ex.category}», по НСПД «{category}»",
                )
            )
    if ex.permitted_use and permitted_use:
        st = "ok" if _similar(ex.permitted_use, permitted_use) else "mismatch"
        out.append(
            CheckItem(
                "vri", "ВРИ", st, f"в документе «{ex.permitted_use}», по НСПД «{permitted_use}»"
            )
        )
    return out


def _fold(s: str) -> str:
    return re.sub(r"\s+", " ", s.lower().replace("ё", "е")).strip()


def _similar(a: str, b: str) -> bool:
    ta = {t for t in re.findall(r"[а-яё]{3,}", a.lower().replace("ё", "е"))}
    tb = {t for t in re.findall(r"[а-яё]{3,}", b.lower().replace("ё", "е"))}
    if not ta or not tb:
        return a.strip().lower() == b.strip().lower()
    return len(ta & tb) / len(ta | tb) >= 0.5 or ta <= tb or tb <= ta


def best_extract(texts: list[str]) -> Extract | None:
    """Из нескольких документов — тот, что больше похож на выписку и полнее заполнен."""
    best: Extract | None = None
    for t in texts:
        ex = parse_extract(t)
        if best is None or (ex.is_extract, ex.filled) > (best.is_extract, best.filled):
            best = ex
    return best


def summary_only(check: dict[str, Any] | None) -> dict[str, Any] | None:
    """После решения модератора — только итог, без строк с масками имён."""
    if not check:
        return None
    return {k: check.get(k) for k in ("verdict", "label", "checked_at") if check.get(k) is not None}
