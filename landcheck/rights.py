"""Права и ограничения участка — первый блок отчёта (ТЗ v3, п. 2).

Все три эксперта на зуме 28.10: «без сведений о правах дальнейший анализ бессмысленен; участок
в аресте — дальше смотреть нечего; муниципальный — свободен или давно в аренде, НСПД этого не
показывает». Источники по убыванию достоверности:

1. электронная выписка ЕГРН (XML Росреестра или PDF с Госуслуг) — `from_extract`;
2. текст страницы ЛК Росреестра «Справочная информация по объектам недвижимости», вставленный
   пользователем (капчу и антибот автоматизировать нельзя) — `parse_lk_text`;
3. карточка НСПД — только форма собственности — `from_nspd`.

ФИО правообладателя-физлица в базу не попадает: храним вид (физлицо/юрлицо/публичное
образование) и маску «И***в И. И.» (`egrn.mask_name`). Юрлица и органы власти — открытые
данные, хранятся как есть.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from landcheck import egrn
from landcheck.models import Level, Purpose

SOURCE_LABELS = {
    "egrn_xml": "выписка ЕГРН (XML Росреестра)",
    "egrn_pdf": "выписка ЕГРН (PDF с Госуслуг)",
    "lk": "ЛК Росреестра, справочная информация (вставлено вручную)",
    "nspd": "НСПД, карточка участка",
}
SOURCE_RANK = {"egrn_xml": 3, "egrn_pdf": 2, "lk": 1, "nspd": 0}
LK_URL = "https://lk.rosreestr.ru/eservices/real-estate-objects-online"

# Виды прав и ограничений: (ключ, подстрока-регулярка, подпись, это ограничение?)
KINDS: tuple[tuple[str, str, str, bool], ...] = (
    ("arrest", r"арест\w*", "арест", True),
    (
        "ban",
        r"запре\w+\s+(?:на\s+)?(?:соверш\w+\s+)?(?:регистрац\w+|действ\w+)|запрещени\w*",
        "запрет регистрационных действий",
        True,
    ),
    ("mortgage", r"ипотек\w*|залог\w*", "ипотека (залог)", True),
    ("lease", r"аренд\w*", "аренда", True),
    ("servitude", r"сервитут\w*", "сервитут", True),
    ("trust", r"доверительн\w+\s+управлени\w*", "доверительное управление", True),
    ("rent", r"\bрент\w*", "рента", True),
    ("tenancy", r"\bна[её]м\w*", "наём", True),
    ("ownership", r"собственност\w*", "собственность", False),
    (
        "permanent_use",
        r"постоянн\w+\s*\(?бессрочн\w*\)?\s*пользовани\w*",
        "постоянное (бессрочное) пользование",
        False,
    ),
    (
        "inherited",
        r"пожизненн\w+\s+наследуем\w+\s+владени\w*",
        "пожизненное наследуемое владение",
        False,
    ),
    ("free_use", r"безвозмездн\w+\s+пользовани\w*", "безвозмездное пользование", False),
    ("economic", r"хозяйственн\w+\s+ведени\w*", "хозяйственное ведение", False),
    ("operative", r"оперативн\w+\s+управлени\w*", "оперативное управление", False),
)
_KIND_RE = re.compile("|".join(f"(?P<{k}>{rx})" for k, rx, _, _ in KINDS), re.I)
_LABEL = {k: label for k, _, label, _ in KINDS}
_IS_RESTRICTION = {k: r for k, _, _, r in KINDS}
SEVERE = ("arrest", "ban")

_NUMBER_RE = re.compile(r"№\s*([0-9][0-9:\-/.]*[0-9])")
_DATE_RE = re.compile(r"\b(\d{2}\.\d{2}\.\d{4})\b")
_TERM_RE = re.compile(
    r"(?:срок[^:\n]{0,40}:?\s*)?(?:с\s+(\d{2}\.\d{2}\.\d{4})\s+)?(?:по|до)\s+(\d{2}\.\d{2}\.\d{4})|"
    r"на\s+(\d+)\s+(?:лет|год\w*)|(бессрочн\w*)",
    re.I,
)
_HOLDER_RE = re.compile(
    r"(?:в\s+пользу\s+(?:которого|кого)[^:\n]{0,80}:|лицо[^:\n]{0,140}:|правообладател\w*\s*:)"
    r"\s*([^\n;]{3,160})",
    re.I,
)
_PUBLIC_RE = re.compile(
    r"(российская\s+федерация|субъект|област\w*|край|республик\w*|муниципальн\w*|"
    r"администраци\w*|городско\w+\s+округ|район\w*|поселени\w*|публично-правов\w*)",
    re.I,
)
_NO_RESTRICTIONS_RE = re.compile(
    r"(ограничени\w*\s+прав\s+и\s+обременени\w*[^\n:]{0,60}:?\s*(?:не\s+зарегистрирован\w*|"
    r"отсутству\w*|данные\s+отсутствуют|нет))",
    re.I,
)
_SECTION_RE = re.compile(
    r"(ограничени\w*\s+прав\s+и\s+обременени\w*|сведения\s+о\s+правах\s+и\s+ограничени\w*|"
    r"вид,?\s*номер\s*и\s*дата\s*(?:государственной\s*)?регистрации\s*права|правопритязани\w*|"
    r"заявленн\w+\s+в\s+судебном\s+порядке)",
    re.I,
)


@dataclass
class Entry:
    """Одно право или ограничение."""

    kind: str
    label: str
    number: str | None = None
    date: str | None = None
    term: str | None = None
    holder: str | None = None  # маска для физлица, название для юрлица или органа
    holder_kind: str | None = None  # person|company|public
    note: str | None = None

    @property
    def restriction(self) -> bool:
        return _IS_RESTRICTION.get(self.kind, False)


@dataclass
class Rights:
    source: str  # egrn_xml|egrn_pdf|lk|nspd
    as_of: str | None = None  # дата сведений (dd.mm.yyyy)
    fetched_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    rights: list[Entry] = field(default_factory=list)
    restrictions: list[Entry] = field(default_factory=list)
    restrictions_known: bool = False  # раздел ограничений прочитан (пустой — тоже знание)
    claims: str | None = None  # правопритязания / заявленные в судебном порядке права
    ownership_type: str | None = None  # форма собственности по НСПД
    parsed: bool = True  # из текста удалось что-то прочитать
    notes: list[str] = field(default_factory=list)

    @property
    def source_label(self) -> str:
        return SOURCE_LABELS.get(self.source, self.source)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["source_label"] = self.source_label
        return d


def holder_kind(name: str | None) -> str | None:
    if not name or not name.strip():
        return None
    if _PUBLIC_RE.search(name):
        return "public"
    if egrn._COMPANY_RE.search(name):  # noqa: SLF001 — общий словарь юрлиц
        return "company"
    return "person"


def safe_holder(name: str | None) -> tuple[str | None, str | None]:
    """(что хранить, вид): физлицо — только маска, остальные — как есть."""
    kind = holder_kind(name)
    if kind is None:
        return None, None
    name = re.sub(r"\s+", " ", name or "").strip(" ,;:")
    if kind == "person":
        return egrn.mask_name(name), kind
    return name[:160], kind


def _entries_from_text(text: str) -> list[Entry]:
    """Все упоминания «<вид> № <номер> от <дата>» в тексте, с попыткой подобрать срок и лицо."""
    out: list[Entry] = []
    seen: set[tuple[str, str | None]] = set()
    for m in _KIND_RE.finditer(text):
        kind = m.lastgroup or ""
        window = text[m.end() : m.end() + 420]
        # окно до следующего вида права, чтобы номер соседней записи не приклеился
        nxt = _KIND_RE.search(window)
        if nxt and nxt.start() > 0:
            window = window[: nxt.start()]
        num = _NUMBER_RE.search(window)
        dates = _DATE_RE.findall(window)
        number = num.group(1) if num else None
        key = (kind, number)
        if key in seen:
            continue
        seen.add(key)
        term = None
        tm = _TERM_RE.search(window)
        if tm:
            if tm.group(2):
                term = (f"с {tm.group(1)} " if tm.group(1) else "") + f"по {tm.group(2)}"
            elif tm.group(3):
                term = f"на {tm.group(3)} лет"
            elif tm.group(4):
                term = "бессрочно"
        hm = _HOLDER_RE.search(window)
        holder, hk = safe_holder(hm.group(1)) if hm else (None, None)
        out.append(
            Entry(
                kind=kind,
                label=_LABEL[kind],
                number=number,
                date=dates[0] if dates else None,
                term=term,
                holder=holder,
                holder_kind=hk,
            )
        )
    return out


def parse_lk_text(text: str) -> Rights:
    """Текст страницы ЛК Росреестра «Справочная информация по объектам недвижимости» (видео
    зума 28.10, 12:15: «Сведения о правах и ограничениях (обременениях): Вид, номер и дата
    государственной регистрации права — Собственность № 40-40-10/002/2013-603 от 07.03.2013»).
    Та же функция читает любой вставленный текст выписки."""
    r = Rights(source="lk")
    t = (text or "").replace("\xa0", " ")
    t = re.sub(r"[ \t]+", " ", t)
    if not t.strip():
        r.parsed = False
        return r
    section = _SECTION_RE.search(t)
    entries = _entries_from_text(t)
    r.rights = [e for e in entries if not e.restriction]
    r.restrictions = [e for e in entries if e.restriction]
    r.restrictions_known = (
        bool(_NO_RESTRICTIONS_RE.search(t))
        or bool(r.restrictions)
        or bool(re.search(r"ограничени\w*\s+прав\s+и\s+обременени", t, re.I))
    )
    cm = re.search(
        r"(?:правопритязани\w*|заявленн\w+\s+в\s+судебном\s+порядке\s+прав\w*)[^:\n]{0,40}:\s*"
        r"([^\n]{2,160})",
        t,
        re.I,
    )
    if cm:
        val = cm.group(1).strip()
        r.claims = None if re.search(r"отсутству|нет|не\s+зарегистр", val, re.I) else val
    dm = re.search(
        r"(?:дата\s+(?:обновления|актуальности|формирования)[^\d]{0,40}|по\s+состоянию\s+на\s+)"
        r"(\d{2}\.\d{2}\.\d{4})",
        t,
        re.I,
    )
    r.as_of = dm.group(1) if dm else None
    r.parsed = bool(section or entries)
    if not r.parsed:
        r.notes.append("в тексте не нашёл раздела о правах: вставьте страницу целиком")
    return r


def from_extract(ex: egrn.Extract) -> Rights:
    """Права из разобранной выписки ЕГРН (XML или PDF)."""
    r = Rights(source="egrn_xml" if ex.source == "xml" else "egrn_pdf")
    r.as_of = ex.issued or (ex.issued_at[:10] if ex.issued_at else None)
    if r.as_of and "-" in r.as_of:
        y, mo, d = r.as_of.split("-")[:3]
        r.as_of = f"{d}.{mo}.{y}"
    if ex.right_kind:
        km = _KIND_RE.search(ex.right_kind)
        kind = km.lastgroup if km else "ownership"
        holder, hk = safe_holder(ex.holders[0]) if ex.holders else (None, None)
        if len(ex.holders) > 1:
            holder = f"{holder} и ещё {len(ex.holders) - 1}" if holder else f"{len(ex.holders)} лиц"
        r.rights.append(
            Entry(
                kind=kind or "ownership",
                label=_LABEL.get(kind or "ownership", ex.right_kind),
                number=ex.right_number,
                date=ex.right_date,
                holder=holder,
                holder_kind=hk,
            )
        )
    r.restrictions_known = ex.encumbrances_known
    for enc in ex.encumbrances:
        km = _KIND_RE.search(enc)
        kind = km.lastgroup if km else None
        if kind and _IS_RESTRICTION.get(kind):
            r.restrictions.append(Entry(kind=kind, label=_LABEL[kind]))
        elif enc:
            r.restrictions.append(Entry(kind="other", label=enc[:80]))
    r.parsed = ex.is_extract or bool(ex.right_kind)
    return r


def from_nspd(parcel: dict[str, Any]) -> Rights:
    r = Rights(source="nspd", ownership_type=parcel.get("ownership_type"))
    r.as_of = (parcel.get("fetched_at") or "")[:10] or None
    if r.as_of and "-" in r.as_of:
        y, mo, d = r.as_of.split("-")[:3]
        r.as_of = f"{d}.{mo}.{y}"
    r.restrictions_known = False
    return r


def _public_ownership(text: str | None) -> bool:
    o = (text or "").lower()
    return any(
        k in o for k in ("публичн", "государств", "муниципальн", "не разграничен", "федеральн")
    )


def summarize(
    sources: list[Rights], parcel: dict[str, Any] | None = None, purpose: Purpose = Purpose.ANY
) -> dict[str, Any]:
    """Одна картина по лучшему источнику: статус, заголовок, строки и уровень для вердикта.

    status: arrest | leased | private | public_free | public_unknown | unknown
    """
    nspd = from_nspd(parcel or {}) if parcel else None
    pool = [s for s in sources if s.parsed] + ([nspd] if nspd else [])
    if not pool:
        return {
            "status": "unknown",
            "level": Level.CHECK.value,
            "headline": "сведений о правах нет",
        }
    best = max(pool, key=lambda s: SOURCE_RANK.get(s.source, -1))
    restrictions = list(best.restrictions)
    severe = [e for e in restrictions if e.kind in SEVERE]
    leases = [e for e in restrictions if e.kind == "lease"] + [
        e for e in best.rights if e.kind == "lease"
    ]
    owner = next((e for e in best.rights if not e.restriction and e.kind != "lease"), None)
    # Собственник назван органом власти — публичная; не назван (ЛК Росреестра ФИО не
    # показывает) — по форме собственности из НСПД.
    nspd_public = _public_ownership(best.ownership_type or (parcel or {}).get("ownership_type"))
    public_owner = (
        owner.holder_kind == "public" if owner is not None and owner.holder_kind else nspd_public
    )
    if severe:
        e = severe[0]
        status, level = "arrest", Level.STOP
        headline = (
            f"{e.label}"
            + (f" № {e.number}" if e.number else "")
            + (f" от {e.date}" if e.date else "")
        )
        headline += ": сделка и регистрация прав невозможны до снятия"
    elif leases:
        e = leases[0]
        status, level = "leased", Level.CHECK
        headline = (
            "участок в аренде"
            + (f" у {e.holder}" if e.holder else "")
            + (f" {e.term}" if e.term else "")
        )
        headline += (
            "; муниципалитет распорядиться им не может — работать с арендатором (переуступка с "
            "согласия арендодателя)"
            if public_owner
            else "; покупка — только с учётом договора аренды"
        )
    elif best.source == "nspd":
        if public_owner:
            status, level = "public_unknown", Level.CHECK
            headline = (
                f"собственность «{best.ownership_type}» по НСПД: свободен участок или давно "
                "в аренде — НСПД не показывает, нужна выписка ЕГРН или справка из ЛК Росреестра"
            )
        elif best.ownership_type:
            status, level = "private", Level.CHECK
            headline = (
                f"собственность «{best.ownership_type}» по НСПД: обременения и аресты видны только "
                "в выписке ЕГРН"
            )
        else:
            status, level = "unknown", Level.CHECK
            headline = "сведений о правах нет: заказать выписку ЕГРН об объекте"
    elif public_owner:
        if best.restrictions_known and not restrictions:
            status, level = "public_free", Level.OK
            headline = (
                "публичная собственность, на дату сведений аренды и обременений нет — участок "
                "свободен: оформление через торги или предварительное согласование (ст. 39.11, "
                "39.15, 39.18 ЗК РФ)"
            )
        else:
            status, level = "public_unknown", Level.CHECK
            headline = "публичная собственность; раздел обременений в документе не прочитан"
    elif owner is not None:
        status = "private"
        level = Level.CHECK if restrictions else Level.OK
        who = {"person": "физлицо", "company": "юрлицо", None: "правообладатель"}.get(
            owner.holder_kind, "правообладатель"
        )
        headline = f"{owner.label}, {who}" + (f" ({owner.holder})" if owner.holder else "")
        headline += (f", № {owner.number}" if owner.number else "") + (
            f" от {owner.date}" if owner.date else ""
        )
        headline += (
            "; обременения: " + ", ".join(dict.fromkeys(e.label for e in restrictions))
            if restrictions
            else ("; обременений нет" if best.restrictions_known else "; обременения не прочитаны")
        )
    else:
        status, level = "unknown", Level.CHECK
        headline = "в документе не нашёл записи о праве: проверьте текст или приложите выписку"
    return {
        "status": status,
        "level": level.value,
        "headline": headline,
        "source": best.source,
        "source_label": best.source_label,
        "as_of": best.as_of,
        "fetched_at": best.fetched_at,
        "rights": [asdict(e) for e in best.rights],
        "restrictions": [asdict(e) for e in restrictions],
        "restrictions_known": best.restrictions_known,
        "claims": best.claims,
        "ownership_type": best.ownership_type or (parcel or {}).get("ownership_type"),
        "notes": best.notes,
        "badge": BADGES[status],
    }


BADGES = {
    "arrest": "арест",
    "leased": "аренда",
    "private": "частная",
    "public_free": "свободен",
    "public_unknown": "муниципальная?",
    "unknown": "права?",
}

SRC_RIGHTS = "выписка ЕГРН / ЛК Росреестра, раздел «Права и ограничения»"
SRC_ZK = (
    "ст. 39.11, 39.15, 39.18 ЗК РФ (торги, предварительное согласование); ст. 22 ЗК РФ (аренда)"
)


def rights_flag(summary: dict[str, Any]) -> dict[str, Any] | None:
    """Флаг R18 по сведениям о правах. Для статуса только по НСПД флага нет — это R12."""
    if summary.get("source") in (None, "nspd"):
        return None
    status = summary["status"]
    level = summary["level"]
    msg = summary["headline"]
    source = SRC_RIGHTS + (f", сведения на {summary['as_of']}" if summary.get("as_of") else "")
    if status in ("public_free", "leased"):
        source += "; " + SRC_ZK
    extra = [e for e in summary.get("restrictions") or [] if e["kind"] not in ("lease", *SEVERE)]
    if extra and status != "arrest":
        msg += "; также: " + ", ".join(dict.fromkeys(e["label"] for e in extra))
        if level == Level.OK.value:
            level = Level.CHECK.value
    return {
        "rule_id": "R18",
        "level": level,
        "message": msg,
        "source": source,
        "details": {
            "status": status,
            "as_of": summary.get("as_of"),
            "rights_source": summary.get("source"),
        },
    }


def apply_to_item(
    data: dict[str, Any], rights: Rights, purpose: Purpose = Purpose.ANY
) -> dict[str, Any]:
    """Сохранить сведения в данные участка, пересобрать флаги R18 и вердикт."""
    data = dict(data)
    stored = [
        Rights(**{k: v for k, v in s.items() if k in Rights.__dataclass_fields__})
        for s in data.get("rights_sources") or []
    ]
    for s in stored:
        s.rights = [Entry(**e) for e in s.rights]  # type: ignore[arg-type]
        s.restrictions = [Entry(**e) for e in s.restrictions]  # type: ignore[arg-type]
    stored = [s for s in stored if s.source != rights.source] + [rights]
    data["rights_sources"] = [s.to_dict() for s in stored]
    summary = summarize(stored, data.get("parcel") or {}, purpose)
    data["rights"] = summary
    flags = [f for f in data.get("flags") or [] if f.get("rule_id") != "R18"]
    flag = rights_flag(summary)
    if flag:
        flags.append(flag)
    data["flags"] = flags
    data["verdict"] = verdict_of(flags)
    return data


def verdict_of(flags: list[dict[str, Any]]) -> str:
    rank = {Level.STOP.value: 2, Level.CHECK.value: 1, Level.OK.value: 0}
    worst = Level.OK.value
    for f in flags:
        if rank.get(f.get("level"), 0) > rank[worst]:
            worst = f["level"]
    return worst
