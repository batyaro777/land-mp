"""Документы администрации по территории участка (ТЗ v3, п. 2.5).

Зум 28.10. Андрей: проекты планировки и межевания, ПЗЗ и градрегламенты лежат на сайте
администрации («Градостроительство → Планировка территории → Проекты планировки и межевания»),
«спрятаны так, что два-три дня ищешь»; по телефону администрация всё знает, письменно отвечает
только после угрозы судом. Влад: к каждому участку должна быть привязана ссылка на регламент его
муниципалитета, «в администрации процесс прошёл, а в Росреестре его ещё нет».

Первый этап: муниципалитет участка, ссылки из реестра, автопоиск официальных документов по видам,
чек-лист Андрея. Разбор самих документов — этап 2 (п. 2.5).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from landcheck import pzz, websearch

SOURCE = "поиск по официальным сайтам: администрация, ГИСОГД, ФГИС ТП, правовые базы"

# вид документа → (подпись, ключевые слова в заголовке/сниппете, шаблон запроса)
KINDS: tuple[tuple[str, str, tuple[str, ...], str], ...] = (
    (
        "pmt",
        "Проекты планировки и межевания территории",
        ("межевани", "планировк", "ппт", "пмт"),
        "{muni} проект межевания территории проект планировки администрация",
    ),
    (
        "pzz",
        "Правила землепользования и застройки, градостроительный регламент",
        ("землепользован", "пзз", "градостроительн", "регламент"),
        "{muni} правила землепользования и застройки градостроительный регламент",
    ),
    (
        "genplan",
        "Генеральный план",
        ("генеральн", "генплан"),
        "{muni} генеральный план документ территориального планирования",
    ),
    (
        "npa",
        "Постановления: изменения проектов межевания, резервирование, изъятие",
        ("постановлен", "резервирован", "изъяти", "решение", "распоряжен"),
        "{muni} постановление администрации проект межевания изменения резервирование земель",
    ),
    (
        "hearings",
        "Публичные слушания и общественные обсуждения",
        ("слушани", "обсуждени"),
        "{muni} публичные слушания генеральный план правила землепользования проект межевания",
    ),
    (
        "notice",
        "Извещения о предоставлении участков (ст. 39.18 ЗК), программы для льготников",
        ("извещени", "предоставлен", "многодетн", "39.18", "аукцион"),
        "{muni} извещение о предоставлении земельного участка многодетным аукцион администрация",
    ),
    (
        "gisogd",
        "ГИСОГД региона и ФГИС ТП",
        ("гисогд", "исогд", "фгис тп", "территориального планирования"),
        "ГИСОГД {region} градостроительная информационная система",
    ),
)
KIND_TITLE = {k: title for k, title, _, _ in KINDS}

OFFICIAL_HINTS = (
    ".gov.ru",
    "gosuslugi.ru",
    "gosweb",
    "garant.ru",
    "consultant.ru",
    "pravo.gov.ru",
    "fgistp",
    "isogd",
    "gisogd",
    "torgi.gov.ru",
    "pravo-search",
    "docs.cntd.ru",
)
OFFICIAL_RE = re.compile(r"adm|admin|rayon|r-n|gorod|mo-|municip|sovet|duma|город|район", re.I)
JUNK_HOSTS = (
    "avito.",
    "cian.",
    "domclick.",
    "yandex.",
    "google.",
    "vk.com",
    "dzen.",
    "youtube.",
    "wikipedia.",
)

CHECKLIST: tuple[tuple[str, str], ...] = (
    (
        "Проекты планировки и межевания (ППТ/ПМТ)",
        "на сайте администрации: «Градостроительство → Планировка территории → Проекты "
        "планировки и межевания». Смотреть приложения: массивы и перечни образуемых участков, "
        "кому распределены (многодетные, ветераны боевых действий, дети войны), под аукцион. "
        "Участок в ПМТ — занят, даже если в ЕГРН свободен (Андрей, зум 28.10)",
    ),
    (
        "ПЗЗ и градостроительный регламент",
        "карта зон и таблица видов использования по зоне: код ВРИ в ЕГРН не равен тому, что "
        "разрешает регламент; у сельских поселений — свои ПЗЗ. Данные администрации свежее "
        "Росреестра (Влад)",
    ),
    (
        "Постановления по территории",
        "изменения ПМТ («проект изменён, № 207 от 7 ноября» — ищите на сайте), резервирование и "
        "изъятие для государственных нужд, перевод категорий, отказ в предварительном согласовании",
    ),
    (
        "Публичные слушания",
        "анонсы изменений генплана, ПЗЗ, ППТ/ПМТ: то, что станет обременением через месяцы",
    ),
    (
        "Извещения о предоставлении (ст. 39.18 ЗК) и программы",
        "30 дней на заявки других желающих; льготные программы («Земля за 1 рубль», многодетным) "
        "меняют, кому достанется участок",
    ),
)


def host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def is_official(url: str) -> bool:
    host = host_of(url)
    if not host or any(j in host for j in JUNK_HOSTS):
        return False
    if any(h in host for h in OFFICIAL_HINTS):
        return True
    return bool(OFFICIAL_RE.search(host))


def classify(title: str, snippet: str, default: str) -> str:
    """Вид документа по словам заголовка/сниппета; вид запроса в приоритете, если его слова есть."""
    text = f"{title} {snippet}".lower()
    keys_by_kind = {kind: keys for kind, _, keys, _ in KINDS}
    if any(k in text for k in keys_by_kind.get(default, ())):
        return default
    for kind, _, keys, _ in KINDS:
        if any(k in text for k in keys):
            return kind
    return default


_MUNICIPAL_RE = re.compile(r"район|округ|\bг\.|город|поселени|сельсовет|волость|улус|кожуун", re.I)


def _looks_municipal(name: str | None) -> bool:
    return bool(name) and bool(_MUNICIPAL_RE.search(name))


_ADDRESS_MUNI_RE = re.compile(
    r"([А-ЯЁ][а-яё-]+(?:ий|ой|ая)\s+(?:район|муниципальный округ|городской округ))"
)


def municipality_for(parcel: dict[str, Any], locality: str | None) -> dict[str, Any]:
    """Муниципалитет участка: из реестра ПЗЗ (точнее), иначе район/город из адреса НСПД."""
    links = pzz.links_for(parcel)
    muni = links.get("municipal")
    name = None
    if muni:
        name = str(muni["name"]).split("(")[0].strip()
    elif locality:
        from landcheck.region import district_of, short_place

        name = district_of(locality) or short_place(locality)
        if not _looks_municipal(name):
            name = None
    if not name and parcel.get("address"):
        m = _ADDRESS_MUNI_RE.search(parcel["address"])
        name = m.group(1) if m else None
    return {
        "name": name,
        "region": links.get("region"),
        "registry": muni,
        "regional": links.get("regional") or [],
        "federal": links.get("federal") or [],
        "checked": links.get("checked"),
    }


def discover(
    muni_name: str,
    region: str | None,
    *,
    search: Callable[..., dict[str, Any]] | None = None,
    max_per_kind: int = 4,
) -> dict[str, Any]:
    """Официальные документы по видам через поисковый движок стека (SearXNG → DDGS)."""
    search = search or websearch.search  # разрешается при вызове — удобно подменять в тестах
    docs: dict[str, list[dict[str, Any]]] = {k: [] for k, *_ in KINDS}
    seen: set[str] = set()
    errors: list[str] = []
    engine = None
    for kind, _, _, template in KINDS:
        if kind == "gisogd" and not region:
            continue
        query = template.format(muni=muni_name, region=region or "")
        try:
            res = search(query, max_results=8)
        except Exception as exc:  # noqa: BLE001 — поиск не должен валить отчёт
            errors.append(f"{kind}: {type(exc).__name__}")
            continue
        engine = engine or res.get("engine")
        if res.get("error"):
            errors.append(f"{kind}: {res['error']}")
        for r in res.get("results") or []:
            url = (r.get("url") or "").strip()
            if not url or url in seen or not is_official(url):
                continue
            k = classify(r.get("title") or "", r.get("snippet") or "", kind)
            if len(docs[k]) >= max_per_kind:
                continue
            seen.add(url)
            docs[k].append(
                {
                    "title": (r.get("title") or url)[:160],
                    "url": url,
                    "host": host_of(url),
                    "snippet": (r.get("snippet") or "")[:240],
                    "query_kind": kind,
                }
            )
    total = sum(len(v) for v in docs.values())
    return {
        "docs": {k: v for k, v in docs.items() if v},
        "total": total,
        "engine": engine,
        "errors": errors,
        "checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def summary(
    parcel: dict[str, Any],
    locality: str | None,
    *,
    search: Callable[..., dict[str, Any]] | None = None,
    discovered: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Блок «Документы администрации»: муниципалитет, ссылки реестра, найденное, чек-лист."""
    muni = municipality_for(parcel, locality)
    found = discovered
    if found is None and muni["name"]:
        found = discover(muni["name"], muni["region"], search=search)
    empty = {"docs": {}, "total": 0, "engine": None, "errors": [], "checked_at": None}
    return {
        "municipality": muni["name"],
        "region": muni["region"],
        "registry": muni["registry"],
        "regional": muni["regional"],
        "federal": muni["federal"],
        "registry_checked": muni["checked"],
        "found": found or empty,
        "kind_titles": KIND_TITLE,
        "checklist": [{"what": w, "how": h} for w, h in CHECKLIST],
        "source": SOURCE,
        "note": (
            "Ссылки найдены автоматически по официальным доменам и могут вести на страницу "
            "раздела, а не на конкретный документ: открыть, найти свой населённый пункт и "
            "кадастровый квартал. Разбор самих документов (кадастровые номера в ПМТ, кому "
            "распределены) — следующий этап."
        ),
    }
