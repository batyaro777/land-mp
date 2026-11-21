"""ГПЗУ — градостроительный план земельного участка (форма: приказ Минстроя от 25.04.2017
№ 741/пр, с изм. 2023 — раздел 12). Разбор текстового слоя PDF или вставленного текста,
сверка с карточкой НСПД, флаг R20 (ТЗ v3, п. 3: зум 28.10 — в ГПЗУ администрация пишет
терзону, ЗОУИТ с реестровыми номерами и приводит ВРИ к коду классификатора).

Что берём: шапка (№, кадастровый номер, площадь, местонахождение, дата выдачи), 2.1 реквизиты
ПЗЗ и терзона, 2.2 основные / условно разрешённые / вспомогательные ВРИ, 2.3 предельные
параметры (отступы, этажность, высота, процент застройки), 5–6 ЗОУИТ с номерами, 11 красные
линии, примечания о приведении ВРИ к коду. Файл не храним — только разобранные поля; ФИО
заявителя не извлекаем. Разбор по регуляркам — ориентир, сверять с документом.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from landcheck.models import Level
from landcheck.rules import classifier

SRC_GRK_573 = (
    "ГрК РФ ст. 57.3: ГПЗУ выдаёт орган местного самоуправления по заявлению правообладателя "
    "бесплатно за 14 рабочих дней; сведения действуют 3 года со дня выдачи (ч. 10)"
)
SRC_GPZU = "ГПЗУ, разделы 2.1–2.3, 5–6"
ORDER_HOWTO = (
    "ГПЗУ заказывает собственник участка (или представитель по доверенности) — покупатель "
    "просит его у продавца. Бесплатно, 14 рабочих дней: Госуслуги → услуга «Выдача "
    "градостроительного плана земельного участка» (или МФЦ / администрация). Срок действия "
    "сведений — 3 года"
)
STALE_YEARS = 3

_CAD = r"\d{2}:\d{2}:\d{6,7}:\d+"
_TITLE_RE = re.compile(r"градостроительн\w+\s+план\w*\s+земельн\w+\s+участк\w*", re.I)
_NUMBER_RE = re.compile(
    r"градостроительн\w+\s+план\w*\s+земельн\w+\s+участк\w*\s*(?:№|N|No\.?)\s*([A-Za-zА-Яа-я0-9][\w\-–/]*)",
    re.I,
)
_ISSUED_RE = re.compile(r"дата\s+выдачи\s*:?\s*(\d{2}\.\d{2}\.\d{4})", re.I)
_CAD_FIELD_RE = re.compile(r"кадастровый\s+номер[^\d]{0,160}?(" + _CAD + ")", re.I | re.S)
_AREA_RE = re.compile(
    r"площадь\s+земельного\s+участка\s*:?\s*(\d[\d\s]*(?:[.,]\d+)?)\s*(кв\.?\s*м|м2|м²|га)", re.I
)
_LOCATION_RE = re.compile(
    r"местонахождение\s+земельного\s+участка\s*:?\s*(.+?)(?=\n\s*(?:описание\s+границ|кадастровый\s+номер|площадь)|\Z)",
    re.I | re.S,
)
_ISSUER_RE = re.compile(
    r"((?:администраци|комитет|министерств|департамент|главархитектур|агентств|управлени)\w*[^\n;]{3,110})",
    re.I,
)
_ZONE_RE = re.compile(
    r"(?<![А-Яа-яA-Za-z\d])((?:Ж|О|П|И|ИТ|Т|С|СХ|Сх|Р|К|Ц|Д|Л|ТЗ|ОД|ПК|ПР|ТР|СН|ЗН|ОЖ|ЖО)"
    r"\s?-\s?\d{1,2}(?:[.\-/]\d{1,2})?|(?:Ж|О|П|Р|Т|С|И)\d{1,2}(?:\.\d{1,2})?)(?![\dА-Яа-я])"
)
_ZONE_TEXT_RE = re.compile(
    r"территориальн\w*\s+зон\w*\s*[«\"„кk(]?\s*"
    r"([А-ЯЁ]{1,3}(?:\s?-?\s?\d{1,2}(?:[.\-/]\d{1,2})?)?)\s*[-–—]\s*([^»>\"\n)]{3,80})",
)
_REG_RE = re.compile(r"\b\d{2}:\d{2}-\d{1,2}\.\d{1,6}\b|\b\d{2}\.\d{2}\.\d\.\d{1,6}\b")
_ZOUIT_WORDS = (
    "охранная зона",
    "охранной зоны",
    "санитарно-защитн",
    "водоохранн",
    "прибрежн",
    "приаэродромн",
    "затоплен",
    "подтоплен",
    "зона охраны",
    "регулирования застройки",
    "пояс",
    "минимальных расстояний",
    "сервитут",
    "защитная зона объекта культурного наследия",
    "санитарной охраны",
    "придорожн",
    "охраняем",
    "противопожарн",
    "защитное расстояние",
    "защитном расстоянии",
)
_CODE_NOTE_RE = re.compile(
    r"[^\n.;]*(?:привед\w+[^\n.;]{0,80}(?:соответств|классификатор)|классификатор\w*[^\n.;]{0,60}код\w*\s*\d+\.\d+|код\w*\s*\d+\.\d+[^\n.;]{0,60}классификатор)[^\n.;]*",
    re.I,
)

# Заголовки разделов формы — границы для вырезания блоков.
_HEADINGS: dict[str, str] = {
    "2.1": r"2\.1\.?\s*реквизиты\s+акта",
    "2.2": r"2\s?\.\s?2\.?\s*информац\w*\s+о\s+вид\w*\s+р\w{0,3}реш",
    "2.3": r"2\.3\.?\s*предельные",
    "2.4": r"2\.4\.?\s*требования\s+к\s+назначению",
    "2.5": r"2\.5\.?\s*",
    "3": r"(?<!\d\.)3\.\s*информация\s+о\s+расположенных",
    "4": r"(?<!\d\.)4\.\s*информация\s+о\s+расч[её]тных",
    "5": r"(?<!\d\.)5\.\s*информация\s+об\s+ограничениях",
    "6": r"(?<!\d\.)6\.\s*информация\s+о\s+границах\s+зон",
    "7": r"(?<!\d\.)7\.\s*информация\s+о\s+границах\s+публичных",
    "8": r"(?<!\d\.)8\.\s*номер\s+и",
    "9": r"(?<!\d\.)9\.\s*информация\s+о\s+возможности",
    "10": r"(?<!\d\.)10\.\s*реквизиты\s+нормативных",
    "11": r"(?<!\d\.)11\.\s*информация\s+о\s+красных",
    "12": r"(?<!\d\.)12\.\s*информация\s+о\s+требованиях",
}
# OCR сканов портит «разрешённого» («ршрешенного», «рЕврешенного» — ГПЗУ Карелии, 07.11)
_SUB_22 = {
    "main": r"основн\w*\s+вид\w*\s+р\w{0,3}реш\w*\s+использовани\w*",
    "conditional": r"условно\s+р\w{0,3}реш\w*\s+вид",
    "aux": r"вспомогательн\w*\s+вид",
}


@dataclass
class Gpzu:
    is_gpzu: bool = False
    number: str | None = None
    cad_num: str | None = None
    area_m2: float | None = None
    location: str | None = None
    issued: str | None = None
    issuer: str | None = None
    pzz_act: str | None = None
    zone_code: str | None = None
    zone_name: str | None = None
    vri_main: list[str] = field(default_factory=list)
    vri_conditional: list[str] = field(default_factory=list)
    vri_aux: list[str] = field(default_factory=list)
    codes_main: list[str] = field(default_factory=list)
    codes_conditional: list[str] = field(default_factory=list)
    setbacks: str | None = None
    floors: str | None = None
    height: str | None = None
    coverage_pct: str | None = None
    zouit: list[dict[str, str | None]] = field(default_factory=list)
    red_lines: bool | None = None
    public_servitudes: bool | None = None
    code_notes: list[str] = field(default_factory=list)
    sections: list[str] = field(default_factory=list)
    text_len: int = 0

    @property
    def filled(self) -> int:
        return sum(
            1
            for v in (
                self.cad_num,
                self.zone_code,
                self.vri_main,
                self.setbacks,
                self.floors,
                self.zouit,
                self.issued,
            )
            if v
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _clean(t: str) -> str:
    t = t.replace("\xa0", " ").replace("\r", "")
    t = re.sub(r"[ \t]+", " ", t)
    return re.sub(r"\n{3,}", "\n\n", t)


def _sections(t: str) -> dict[str, str]:
    """Текст каждого раздела формы: от его заголовка до следующего найденного заголовка."""
    hits: list[tuple[int, str]] = []
    for key, rx in _HEADINGS.items():
        m = re.search(rx, t, re.I)
        if m:
            hits.append((m.start(), key))
    hits.sort()
    out: dict[str, str] = {}
    for i, (start, key) in enumerate(hits):
        end = hits[i + 1][0] if i + 1 < len(hits) else len(t)
        out[key] = t[start:end]
    return out


def _lines(block: str) -> list[str]:
    out: list[str] = []
    for raw in block.split("\n"):
        s = re.sub(r"^\s*(?:[-–•·*]|\d{1,2}[).]|\(\d+\))\s*", "", raw).strip(" ;,")
        if len(s) < 4:
            continue
        if re.match(r"^(основные|условно|вспомогательные)\s+виды", s, re.I):
            continue
        if re.match(r"^(наименование|код|описание)\b", s, re.I) and len(s) < 40:
            continue
        out.append(s[:200])
    return out


def _vri_items(block: str) -> list[str]:
    """Строки с ВРИ: убираем служебные, объединяем переносы «...строительства\\n(2.1)»."""
    items: list[str] = []
    for s in _lines(block):
        if re.match(r"^(примечани|сведения ЕГРН|по сведениям|в соответствии с ч)", s, re.I):
            continue  # примечания администрации о приведении ВРИ к коду — в code_notes
        if items and re.match(r"^\(?\d{1,2}\.\d{1,2}(?:\.\d{1,2})?\)?$", s):
            items[-1] = f"{items[-1]} ({s.strip('()')})"
            continue
        if re.match(r"^\d{1,2}\.\d{1,2}", s) and len(s) < 10:
            continue
        items.append(s)
    return items[:40]


# Код ВРИ отдельной строкой в таблице 2.2; OCR путает 1 с «|», «l», «I» и 3 с «З»
_CODE_LINE_RE = re.compile(r"^\s*([\dlI|Зз]{1,2}\.[\dlI|Зз]{1,2}(?:\.[\dlI|Зз]{1,2})?)\s*\*?\s*$")
_OCR_DIGITS = str.maketrans({"l": "1", "I": "1", "|": "1", "З": "3", "з": "3"})


def _table_codes(block: str) -> list[str]:
    """Коды ВРИ, стоящие в таблице отдельными строками (колонка «Код вида»)."""
    out: list[str] = []
    known = classifier.by_code()
    for line in block.split("\n"):
        m = _CODE_LINE_RE.match(line)
        if not m:
            continue
        code = m.group(1).translate(_OCR_DIGITS)
        if code in known and code not in out:
            out.append(code)
    return out


def _coded_items(codes: list[str]) -> list[str]:
    known = classifier.by_code()
    return [f"{known[c].name} ({c})" for c in codes if c in known]


def _vri_part(chunk: str) -> list[str]:
    """Строки ВРИ раздела; таблица «наименование — описание — код» (сканы) даёт мусор из
    описаний, поэтому при двух и больше кодах отдельными строками берём коды и названия
    из классификатора."""
    codes = _table_codes(chunk)
    if len(codes) >= 2:
        return _coded_items(codes)
    return _vri_items(chunk)


def _split_22(block: str) -> dict[str, list[str]]:
    hits: list[tuple[int, str]] = []
    for key, rx in _SUB_22.items():
        m = re.search(rx, block, re.I)
        if m:
            hits.append((m.start(), key))
    hits.sort()
    out: dict[str, list[str]] = {"main": [], "conditional": [], "aux": []}
    if not hits:
        # подзаголовков нет — всё считаем основными
        body = re.sub(_HEADINGS["2.2"] + r"[^\n]*", "", block, count=1, flags=re.I)
        out["main"] = _vri_part(body)
        return out
    for i, (start, key) in enumerate(hits):
        end = hits[i + 1][0] if i + 1 < len(hits) else len(block)
        chunk = block[start:end]
        chunk = re.sub(_SUB_22[key] + r"[^\n]*", "", chunk, count=1, flags=re.I)
        out[key] = _vri_part(chunk)
    return out


def _param(block: str, rx: str) -> str | None:
    m = re.search(rx, block, re.I | re.S)
    if not m:
        return None
    val = re.sub(r"\s+", " ", m.group(1)).strip(" .,;:—-")
    return val[:120] or None


# Строки абзаца о зоне, которые сами зоной не являются (прод 07.11: одна противопожарная зона
# давала пять «ЗОУИТ» — «Согласно п. 4.14 СП…», «Сокращение противопожарных расстояний…»)
_ZOUIT_NOT_START = re.compile(
    r"^(согласно|в\s+соответствии|сокращени|допускается|при\s+условии|требовани|"
    r"в\s+целях|для\s+обеспечения)",
    re.I,
)
# Зоны одного вида без реестрового номера — считаем одной
_ZOUIT_SINGLE = ("противопожарн", "защитное расстояние", "защитном расстоянии")


def _zouit(blocks: list[str]) -> list[dict[str, str | None]]:
    out: list[dict[str, str | None]] = []
    seen: set[str] = set()
    for block in blocks:
        for line in _lines(block):
            lo = line.lower()
            regs = _REG_RE.findall(line)
            if not regs and not any(w in lo for w in _ZOUIT_WORDS):
                continue
            if not regs and len(line) < 20:
                continue  # обрывок ячейки таблицы скана («противопожарное») — не зона
            if not regs and (not line[:1].isupper() or _ZOUIT_NOT_START.match(line)):
                continue  # продолжение абзаца или ссылка на норматив, а не новая зона
            root = next((r for r in _ZOUIT_SINGLE if r in lo), None)
            if not regs and root:
                if root in seen:
                    continue  # одна и та же зона, абзац порезан на строки (ГПЗУ Карелии)
                seen.add(root)
            if re.match(r"^(информация|наименование зоны|обозначение|x\b|y\b)", lo):
                continue
            if re.fullmatch(r"[\d\s.,XYxy-]+", line):
                continue  # строка координат
            name = re.sub(r"\s*\(?\s*(?:реестров\w+|уч[её]тн\w+)\s+номер\w*[^)]*\)?", "", line)
            name = re.sub(r"\s+", " ", name).strip(" :;,—-")[:200]
            key = (regs[0] if regs else name).lower()
            if key in seen:
                continue
            seen.add(key)
            out.append({"name": name, "reg_number": regs[0] if regs else None})
    return out[:30]


def parse(text: str) -> Gpzu:
    g = Gpzu()
    if not text or not text.strip():
        return g
    t = _clean(text)
    g.text_len = len(t)
    g.is_gpzu = _TITLE_RE.search(t) is not None and (
        re.search(_HEADINGS["2.2"], t, re.I) is not None
        or re.search(r"градостроительн\w+\s+регламент", t, re.I) is not None
    )
    if not g.is_gpzu:
        return g
    if m := _NUMBER_RE.search(t):
        g.number = m.group(1).strip("-–/")[:60]
    if m := _ISSUED_RE.search(t):
        g.issued = m.group(1)
    if m := _CAD_FIELD_RE.search(t):
        g.cad_num = m.group(1)
    elif m := re.search(_CAD, t):
        g.cad_num = m.group(0)
    if m := _AREA_RE.search(t):
        try:
            val = float(m.group(1).replace(" ", "").replace(",", "."))
            g.area_m2 = val * 10_000 if m.group(2).lower() == "га" else val
        except ValueError:
            g.area_m2 = None
    if m := _LOCATION_RE.search(t):
        g.location = re.sub(r"\s+", " ", m.group(1)).strip(" ,;")[:300] or None
    head = t[: max(t.lower().find("1. чертеж"), 0) or 2500]
    if m := _ISSUER_RE.search(head):
        g.issuer = re.sub(r"\s+", " ", m.group(1)).strip(" ,;.")[:120]
    secs = _sections(t)
    g.sections = sorted(secs, key=lambda k: [int(x) for x in k.split(".")])
    if "2.1" in secs:
        body = re.sub(_HEADINGS["2.1"] + r"[^\n]*", "", secs["2.1"], count=1, flags=re.I)
        g.pzz_act = re.sub(r"\s+", " ", body).strip(" ,;:—-")[:400] or None
    if "2.2" not in secs and (m := re.search(_SUB_22["main"], t, re.I)):
        # заголовок 2.2 не распознан (скан) — блок ВРИ от «Основные виды…» до раздела 2.3
        end = t.find(secs["2.3"][:60]) if "2.3" in secs else -1
        secs["2.2"] = t[m.start() : end if end > m.start() else m.start() + 8000]
    zone_src = (secs.get("2.1", "") + "\n" + secs.get("2.2", "")[:600]) or t[:4000]
    if m := _ZONE_TEXT_RE.search(t):
        g.zone_code = re.sub(r"\s+", "", m.group(1))
        g.zone_name = m.group(2).strip(" .;,")[:160] or None
    elif m := _ZONE_RE.search(zone_src):
        g.zone_code = re.sub(r"\s+", "", m.group(1)).replace("-", "-")
        tail = zone_src[m.end() : m.end() + 160].split("\n")[0]
        name = re.sub(r"^[\s—–:.\-)«»\"]+", "", tail).strip(" .;,")
        if len(name) > 6:
            g.zone_name = name[:160]
    if "2.2" in secs:
        parts = _split_22(secs["2.2"])
        g.vri_main, g.vri_conditional, g.vri_aux = parts["main"], parts["conditional"], parts["aux"]
        g.codes_main = _codes(g.vri_main)
        g.codes_conditional = _codes(g.vri_conditional)
    if "2.3" in secs:
        b = secs["2.3"]
        g.setbacks = _param(b, r"отступ\w*[^\d\n]{0,120}?(\d+(?:[.,]\d+)?\s*м(?:етр\w*)?)")
        g.floors = _param(
            b, r"(?:количество\s+этажей|этажност\w*)[^\d\n]{0,80}?(\d{1,2}(?:\s*эт\w*)?)"
        )
        g.height = _param(b, r"высот\w*[^\n]{0,80}?(\d{1,3}(?:[.,]\d)?\s*м(?:етр\w*)?)(?![а-яё])")
        g.coverage_pct = _param(b, r"процент\w*\s+застройки[^\d\n]{0,80}?(\d{1,3}\s*%?)")
    # предельные параметры бывают только в примечании на чертеже (таблица 2.3 скана пустая)
    g.floors = g.floors or _param(
        t, r"(?:количество\s+этажей|этажност\w*)[^\d]{0,40}?(\d{1,2})(?:\s|$|[^\d.,])"
    )
    g.height = g.height or _param(
        t, r"(?:предельн\w*|максимальн\w*)\s+высот\w*.{0,80}?(\d{1,3}(?:[.,]\d)?\s*м)(?![а-яё])"
    )
    g.coverage_pct = g.coverage_pct or _param(t, r"процент\w*\s+застройки[^\d]{0,40}?(\d{1,3}\s*%)")
    g.zouit = _zouit([secs.get("5", ""), secs.get("6", "")])
    if "7" in secs:
        b = secs["7"].lower()
        if re.search(r"отсутств|не установлен|не предусмотр|нет\b", b):
            g.public_servitudes = False
        elif re.search(r"\d+[.,]\d{2}\s+\d+[.,]\d{2}|сервитут", b.split("\n", 1)[-1]):
            g.public_servitudes = True
    if re.search(r"красн\w*\s+лини\w*\s*[-–—:]?\s*не\s+установлен", t, re.I):
        g.red_lines = False
    if "11" in secs:
        b = secs["11"].lower()
        if re.search(r"отсутств|не установлен|не предусмотр|нет", b):
            g.red_lines = False
        elif re.search(r"\d+[.,]\d{2}\s+\d+[.,]\d{2}", b):
            g.red_lines = True
    g.code_notes = [
        re.sub(r"\s+", " ", m.group(0)).strip()[:300]
        for m in _CODE_NOTE_RE.finditer(secs.get("2.2", "") or t)
    ][:5]
    return g


OCR_MIN_TEXT = 300  # меньше — считаем PDF сканом и распознаём


def from_pdf(
    raw: bytes, *, ocr_pages: int = 20, ocr_deadline_s: float = 180.0
) -> tuple[Gpzu, dict[str, Any]]:
    """ГПЗУ из PDF: текстовый слой, а если его нет (скан) — OCR первых страниц. Второе значение —
    как читали: {"ocr": bool, "ocr_note": пометка о нераспознанных страницах}."""
    from landcheck import intake, ocr

    text = intake.pdf_text(raw, max_pages=40)
    meta: dict[str, Any] = {"ocr": False, "ocr_note": None}
    if len(text.strip()) < OCR_MIN_TEXT and ocr.available():
        pages, partial = ocr.pdf_ocr_pages(raw, max_pages=ocr_pages, deadline_s=ocr_deadline_s)
        text = "\n".join(p["text"] for p in pages)
        meta.update(ocr=True, ocr_note=partial)
    return parse(text), meta


def _codes(items: list[str]) -> list[str]:
    out: list[str] = []
    for s in items:
        m = classifier.match(s)
        if m is not None and m.how != "fuzzy" and m.code not in out:
            out.append(m.code)
    return out


def _norm_zone(z: str | None) -> str:
    return re.sub(r"[\s\-–]", "", (z or "").upper())


def _zone_codes_of(parcel: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for z in parcel.get("territorial_zones") or []:
        for src in (z.get("name"), z.get("kind"), z.get("describe")):
            if src and (m := _ZONE_RE.search(str(src))):
                out.append(re.sub(r"\s+", "", m.group(1)))
                break
    return out


def compare(g: Gpzu, parcel: dict[str, Any]) -> list[dict[str, Any]]:
    """Сверка ГПЗУ с карточкой НСПД: участок, ВРИ, терзона, ЗОУИТ, свежесть."""
    rows: list[dict[str, Any]] = []

    def row(field_: str, level: Level, text: str) -> None:
        rows.append({"field": field_, "level": level.value, "text": text})

    cad = parcel.get("cad_num")
    if g.cad_num and cad and g.cad_num != cad:
        row("Участок", Level.STOP, f"ГПЗУ выдан на участок {g.cad_num}, а проверяется {cad}")
    elif g.cad_num:
        row("Участок", Level.OK, "кадастровый номер в ГПЗУ совпадает с проверяемым")
    else:
        row("Участок", Level.CHECK, "кадастровый номер в ГПЗУ не прочитан — сверить вручную")

    nspd_codes = classifier.codes_for(
        " | ".join([parcel.get("permitted_use") or "", *(parcel.get("permitted_use_tab") or [])])
    )
    vri = parcel.get("permitted_use") or ""
    if not g.vri_main:
        row(
            "ВРИ",
            Level.CHECK,
            "виды разрешённого использования в ГПЗУ не разобраны — смотреть раздел 2.2",
        )
    elif nspd_codes and any(c in g.codes_main for c in nspd_codes):
        row("ВРИ", Level.OK, f"ВРИ по ЕГРН «{vri}» входит в основные виды зоны по ГПЗУ")
    elif nspd_codes and any(c in g.codes_conditional for c in nspd_codes):
        row(
            "ВРИ",
            Level.CHECK,
            f"ВРИ по ЕГРН «{vri}» в ГПЗУ только условно разрешённый: нужны публичные слушания "
            "и разрешение администрации",
        )
    elif not nspd_codes:
        row(
            "ВРИ",
            Level.CHECK,
            f"ВРИ по ЕГРН «{vri}» не сопоставлен с кодом — сверить с разделом 2.2 ГПЗУ вручную",
        )
    else:
        row(
            "ВРИ",
            Level.CHECK,
            f"ВРИ по ЕГРН «{vri}» (код {', '.join(nspd_codes)}) не входит в основные виды зоны "
            f"по ГПЗУ ({', '.join(g.codes_main) or 'коды не прочитаны'}): уточнить, не нужна ли "
            "смена ВРИ",
        )

    nspd_zones = _zone_codes_of(parcel)
    if g.zone_code and nspd_zones:
        if _norm_zone(g.zone_code) in {_norm_zone(z) for z in nspd_zones}:
            row("Терзона", Level.OK, f"зона {g.zone_code} в ГПЗУ совпадает с НСПД")
        else:
            row(
                "Терзона",
                Level.CHECK,
                f"в ГПЗУ зона {g.zone_code}, в НСПД {', '.join(nspd_zones)}: расходятся — "
                "верить ГПЗУ (он свежее), но спросить администрацию",
            )
    elif g.zone_code:
        row(
            "Терзона",
            Level.OK,
            f"зона по ГПЗУ: {g.zone_code}"
            + (f" — {g.zone_name}" if g.zone_name else "")
            + " (НСПД зону к участку не привязала)",
        )
    else:
        row(
            "Терзона",
            Level.CHECK,
            "код территориальной зоны в ГПЗУ не прочитан — смотреть раздел 2.1–2.2",
        )

    nspd_regs = {z.get("reg_number") for z in parcel.get("zouit") or [] if z.get("reg_number")}
    g_regs = {z["reg_number"] for z in g.zouit if z.get("reg_number")}
    new = g_regs - nspd_regs
    if g.zouit and new:
        names = "; ".join(
            f"{z['name']} ({z['reg_number']})" for z in g.zouit if z.get("reg_number") in new
        )[:300]
        row("ЗОУИТ", Level.CHECK, f"в ГПЗУ есть зоны, которых нет в НСПД: {names}")
    elif g.zouit:
        row("ЗОУИТ", Level.OK, f"ЗОУИТ по ГПЗУ ({len(g.zouit)}) совпадают с НСПД или без номеров")
    else:
        row("ЗОУИТ", Level.OK, "в разделах 5–6 ГПЗУ ограничений не найдено")

    if g.issued:
        try:
            issued = datetime.strptime(g.issued, "%d.%m.%Y").replace(tzinfo=UTC)
            age_days = (datetime.now(UTC) - issued).days
            if age_days > STALE_YEARS * 365:
                row(
                    "Срок",
                    Level.CHECK,
                    f"ГПЗУ от {g.issued} старше 3 лет: для разрешения на строительство нужен новый",
                )
            else:
                row(
                    "Срок",
                    Level.OK,
                    f"ГПЗУ от {g.issued}, действует до {issued.year + STALE_YEARS} г.",
                )
        except ValueError:
            pass
    return rows


def worst(rows: list[dict[str, Any]]) -> Level:
    lv = Level.OK
    for r in rows:
        if Level(r["level"]).rank > lv.rank:
            lv = Level(r["level"])
    return lv


def flag(g: Gpzu, rows: list[dict[str, Any]]) -> dict[str, Any]:
    lv = worst(rows)
    problems = [r["text"] for r in rows if r["level"] != Level.OK.value]
    head = "ГПЗУ" + (f" № {g.number}" if g.number else "") + (f" от {g.issued}" if g.issued else "")
    head += " приложен"
    if problems:
        msg = head + ": " + "; ".join(problems)[:500]
    else:
        msg = head + ": зона, ВРИ и ЗОУИТ сходятся с НСПД"
        if g.zone_code:
            msg += f" (зона {g.zone_code})"
    return {
        "rule_id": "R20",
        "level": lv.value,
        "message": msg,
        "source": SRC_GPZU + (f", выдан {g.issuer}" if g.issuer else ""),
        "details": {"zone": g.zone_code, "codes_main": g.codes_main, "issued": g.issued},
    }


def apply_to_item(data: dict[str, Any], g: Gpzu) -> dict[str, Any]:
    """Записать ГПЗУ и сверку в данные участка, заменить флаг R20, пересчитать вердикт."""
    from landcheck.rights import verdict_of

    parcel = data.get("parcel") or {}
    rows = compare(g, parcel)
    stored = g.to_dict()
    stored["compare"] = rows
    stored["level"] = worst(rows).value
    stored["attached_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    flags = [f for f in data.get("flags") or [] if f.get("rule_id") != "R20"]
    flags.append(flag(g, rows))
    out = dict(data)
    out["gpzu"] = stored
    out["flags"] = flags
    out["verdict"] = verdict_of(flags)
    return out
