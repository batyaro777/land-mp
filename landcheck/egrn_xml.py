"""Выписка ЕГРН в XML (ФГИС ЕГРН): поля читаются структурно, без распознавания.

Схемы Росреестра отличаются версиями и пространствами имён, поэтому разбор идёт по локальным
именам элементов (cad_number, area/value, readable_address, right_type/value, right_holder,
restriction_encumbrance_type/value, date_formation) и терпит отсутствие любого из них.
Имена элементов взяты из схем extract_about_property_land / extract_base_params_land;
на реальном образце выписки набор путей сверяется и при необходимости дополняется.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime

from defusedxml.ElementTree import ParseError as DefusedParseError
from defusedxml.ElementTree import fromstring as safe_fromstring

from landcheck.egrn import Extract
from landcheck.inputs import CAD_RE

MAX_XML_BYTES = 20 * 1024 * 1024
_FORBIDDEN = (b"<!DOCTYPE", b"<!ENTITY")


def is_xml(raw: bytes) -> bool:
    head = raw.lstrip(b"\xef\xbb\xbf \t\r\n")[:200]
    return head.startswith(b"<?xml") or (head.startswith(b"<") and b">" in head)


def parse_xml_extract(raw: bytes) -> Extract | None:
    """None — это не XML или его не удалось разобрать; иначе Extract с source='xml'."""
    if not raw or len(raw) > MAX_XML_BYTES or not is_xml(raw):
        return None
    if any(tok in raw[:4096] for tok in _FORBIDDEN):
        return None  # DTD/сущности не нужны выписке, а защищаться от них дороже, чем отказать
    try:
        root = safe_fromstring(raw)
    except (DefusedParseError, ET.ParseError, ValueError):
        return None
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    ex = Extract(source="xml")
    tag = root.tag.lower()
    ex.is_extract = "extract" in tag or root.find(".//right_records") is not None
    cad = _text(root, ".//common_data/cad_number") or _text(root, ".//cad_number")
    if cad and CAD_RE.search(cad):
        ex.cad_num = re.sub(r"\s+", "", CAD_RE.search(cad).group(0))
    area = _text(root, ".//params/area/value") or _text(root, ".//area/value")
    if area:
        try:
            ex.area_m2 = float(area.replace(",", ".").replace(" ", ""))
        except ValueError:
            ex.area_m2 = None
    ex.address = (_text(root, ".//readable_address") or "")[:300] or None
    ex.category = _text(root, ".//category/type/value") or None
    ex.permitted_use = (
        _text(root, ".//permitted_use_established/by_document")
        or _text(root, ".//permitted_use/name")
        or _text(root, ".//permitted_use//value")
        or None
    )
    _rights(root, ex)
    _restrictions(root, ex)
    issued = _text(root, ".//date_formation") or _text(root, ".//details_statement//date")
    if issued:
        dt = _parse_dt(issued)
        if dt:
            ex.issued = dt.strftime("%d.%m.%Y")
            ex.issued_at = dt.isoformat()
    return ex


def _rights(root: ET.Element, ex: Extract) -> None:
    records = root.findall(".//right_record")
    if not records:
        return
    first = records[0]
    kind = _text(first, ".//right_type/value") or _text(first, ".//right_type/name")
    ex.right_kind = kind.lower() if kind else None
    reg = _text(first, ".//record_info/registration_date") or _text(first, ".//registration_date")
    if reg:
        dt = _parse_dt(reg)
        ex.right_date = dt.strftime("%d.%m.%Y") if dt else reg[:10]
    ex.right_number = _text(first, ".//right_number") or None
    holders: list[str] = []
    for rec in records:
        for h in rec.findall(".//right_holder"):
            name = _holder_name(h)
            if name and name not in holders:
                holders.append(name)
    ex.holders = holders


def _holder_name(h: ET.Element) -> str:
    ind = h.find(".//individual")
    if ind is not None:
        parts = [_text(ind, "surname"), _text(ind, "name"), _text(ind, "patronymic")]
        return " ".join(p for p in parts if p).strip()
    ent = h.find(".//legal_entity")
    if ent is not None:
        return (_text(ent, ".//name") or _text(ent, ".//full_name") or "юридическое лицо").strip()
    pub = h.find(".//public_formation")
    if pub is not None:
        return (_text(pub, ".//name") or _text(pub, ".//value") or "публичное образование").strip()
    txt = " ".join(t.strip() for t in h.itertext() if t.strip())
    return txt[:200]


def _restrictions(root: ET.Element, ex: Extract) -> None:
    ex.encumbrances_known = True  # в XML отсутствие записей означает «не зарегистрировано»
    kinds: set[str] = set()
    for rec in root.findall(".//restrict_record"):
        kind = (
            _text(rec, ".//restriction_encumbrance_type/value")
            or _text(rec, ".//restriction_encumbrance_type/name")
            or _text(rec, ".//restrictions_encumbrances_data//value")
        )
        if kind:
            kinds.add(kind.lower())
        else:
            kinds.add("есть запись")
    ex.encumbrances = sorted(kinds)


def _text(el: ET.Element, path: str) -> str:
    found = el.find(path)
    if found is None or found.text is None:
        return ""
    return re.sub(r"\s+", " ", found.text).strip()


def _parse_dt(s: str) -> datetime | None:
    s = s.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(s[: 26 if "." in s else 19] if "T" in s else s, fmt)
        except ValueError:
            continue
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return None
