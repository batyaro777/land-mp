"""Регион: инвестиции, дороги, риски и рынок по открытым источникам.

Боль брокера (Антон М., 15.10.2026): «информация есть в открытом доступе, но её надо собирать,
структурировать и догадаться, что собирать».

Два режима (LANDCHECK_REGION):
- ``search`` (по умолчанию, бесплатный стек, решение Тони 19.10): поиск SearXNG/DuckDuckGo →
  страницы → текст → языковая модель через бесплатных провайдеров (``landcheck.llm``) собирает
  шесть разделов, ссылаясь на номера источников;
- ``gemini``: прежний путь через Gemini с инструментом google_search (платный уровень).

Правила честности: без найденных источников блок не отвечает «из памяти» модели; каждый
ответ помечен датой, моделью и запросами; один запрос на населённый пункт (кэш по локации).
"""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime
from typing import Any

import httpx

from landcheck.models import Purpose

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
DEFAULT_MODEL = "gemini-3.8-flash"
# Запасные модели, если основная перегружена (503) или без квоты (429): проверено 15.10.2026.
FALLBACK_MODELS = ("gemini-3.7-flash", "gemini-3.5-flash", "gemini-3.1-flash-lite")
SECTIONS = (
    "Развитие и инвестиции",
    "Дороги и инфраструктура",
    "Негативные факторы",
    "Рынок земли",
    "Статус территории",
    "Вывод для покупателя",
)
SRC = "по открытым источникам; ссылки ниже; не юридическое заключение"
MAX_DOCS = 10
MIN_DOC_CHARS = 300  # короче — модель приписывает такому документу чужие факты (прод 20.10)
MIN_SNIPPET_CHARS = 150

_PROMPT = """Ты аналитик рынка земли в России. Локация: {locality}.
Цель покупателя: {activity}.
Найди в открытых источниках актуальные сведения и изложи по-русски строго по разделам с заголовками:
## Развитие и инвестиции
## Дороги и инфраструктура
## Негативные факторы
## Рынок земли
## Статус территории
## Вывод для покупателя
Правила: только факты из найденных источников, с датами и, где есть, суммами; интересуют планы
на 1–5 лет (инвестпроекты, ОЭЗ и ТОР, курортные кластеры, новые дороги и мосты, газификация,
полигоны отходов, промышленные объекты, застройка). В разделе «Рынок земли» — цены за сотку по
типам (ИЖС, садоводство, сельхоз, рекреация) и динамика за год, если источники их приводят.
Где данных нет — пиши «данных не найдено». Никаких предположений и общих слов. В каждом
разделе 2–5 коротких пунктов, каждый начинается с «- ». В конце список источников не добавляй.
"""

_RAG_SYSTEM = (
    "Ты аналитик рынка земли в России. Отвечай только по приложенным документам, "
    "по-русски (без английских слов), кратко и конкретно, с датами и суммами, где они есть. "
    "Ничего не выдумывай: "
    "если в документах факта нет — пиши «данных не найдено». После каждого пункта ставь номера "
    "документов-источников в квадратных скобках, например [2] или [1, 4]. Сегодня {today}: "
    "важны планы и события последних 1–3 лет и на 1–5 лет вперёд; событие старше трёх лет "
    "упоминай только с годом и не выдавай за актуальное; рекламу санаториев, путеводители и "
    "туристические обзоры не пересказывай. Локация — это сама локация: не пиши "
    "«рядом с городом X», если X и есть локация."
)

_RAG_PROMPT = """Локация: {locality}. Цель покупателя: {activity}.
Собери сводку строго из документов ниже. Верни JSON вида
{{"sections": [{{"title": "<название раздела>", "items": ["<пункт> [n]", ...]}}, ...],
 "used": [n, ...]}}
с шестью разделами ровно в таком порядке и с такими названиями:
{sections}
В каждом разделе 2–5 коротких пунктов (или один пункт «данных не найдено»). Интересуют планы на
1–5 лет: инвестпроекты, ОЭЗ и ТОР, курортные кластеры, дороги и мосты, газификация, полигоны
отходов, промышленные объекты, застройка; в «Рынке земли» — только цены земельных участков
за сотку или гектар по типам (ИЖС, садоводство, сельхоз, рекреация) и их динамика; цены
квартир, номеров и апартаментов туда не включай. Цены из объявлений — это цены предложения,
не сделки: слово «продан» только для подтверждённых сделок (Росреестр, ДОМ.РФ, аналитика);
цена за сотку = цена ÷ площадь в сотках, если пересчёт не сходится с источником — ₽/сот не
приводи. Цифры по региону или республике в целом помечай «(по региону в целом)» и не выдавай
за данные по локации. «Статус территории» — только про саму локацию: генплан, ПЗЗ, границы
населённого пункта, курорт, ООПТ, агломерация; общие советы и обзоры туда не включай.
Скотомогильник (в том числе закрытый — санитарная зона остаётся), полигон, свалка, карьер,
промзона — это «Негативные факторы», не развитие и не улучшение.
«Вывод для покупателя» — 2–3 пункта по цели, на правильном русском без повторов.
Поле "used" — номера документов, на которые ты сослался.

Документы:
{docs}
"""


def locality_from_address(address: str | None) -> str | None:
    """«Российская Федерация, Республика Алтай, Чемальский район, с. Элекмонар, ул. …, з/у 5»
    → «Республика Алтай, Чемальский район, с. Элекмонар» (до улицы, дома, участка)."""
    if not address:
        return None
    parts = [p.strip() for p in address.split(",") if p.strip()]
    parts = [p for p in parts if p.lower() not in ("российская федерация", "россия", "рф")]
    stop = re.compile(
        r"^(ул\.?|улица|пер\.?|переулок|пр-?кт|проспект|ш\.?|шоссе|д\.?\s*\d|дом|з/у|участок|уч\.?|"
        r"стр\.?|строение|кв\.?|тер\.?|снт|тсн|днп|мкр|квартал|линия|проезд|\d)",
        re.IGNORECASE,
    )
    kept: list[str] = []
    for p in parts:
        if stop.match(p):
            break
        kept.append(p)
    kept = kept[:4]
    return ", ".join(kept) if kept else None


NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
_UA = "landcheck/0.1 (land-mp.dnai.engineering; broker land check)"


def locality_from_geometry(
    parcel_geojson: dict[str, Any] | None, *, timeout: float = 10.0
) -> str | None:
    """Населённый пункт по контуру участка через Nominatim (OSM, бесплатно): «Алтайский край,
    Белокуриха». Нужен, когда адрес в НСПД — только регион (участок 22:64:010303:10)."""
    if not parcel_geojson:
        return None
    try:
        from landcheck.location import centroid

        lat, lon = centroid(parcel_geojson)
        resp = httpx.get(
            NOMINATIM_URL,
            params={
                "lat": lat,
                "lon": lon,
                "format": "jsonv2",
                "zoom": 14,
                "accept-language": "ru",
            },
            headers={"User-Agent": _UA},
            timeout=timeout,
        )
        if resp.status_code != 200:
            return None
        addr = (resp.json() or {}).get("address") or {}
    except Exception:  # noqa: BLE001 — сеть, кривая геометрия
        return None
    state = addr.get("state") or addr.get("region")
    district = addr.get("county") or addr.get("municipality") or addr.get("state_district")
    place = addr.get("city") or addr.get("town") or addr.get("village") or addr.get("hamlet")
    parts = [p for p in (state, district, place) if p]
    if place and district and place in district:
        parts = [p for p in (state, place) if p]
    return ", ".join(dict.fromkeys(parts)) if parts else None


def resolve_locality(address: str | None, parcel_geojson: dict[str, Any] | None) -> str | None:
    """Локация для поиска: адрес НСПД до улицы; если в нём только регион — уточняем по контуру."""
    loc = locality_from_address(address)
    if loc and loc.count(",") >= 1:
        return loc
    by_geom = locality_from_geometry(parcel_geojson)
    if by_geom:
        return by_geom
    return loc


def locality_key(locality: str) -> str:
    return re.sub(r"\s+", " ", locality.lower().replace("ё", "е")).strip()


def _split_sections(text: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in text.splitlines():
        s = line.strip()
        m = re.match(r"^#{1,3}\s*(.+?)\s*$", s)
        if m:
            current = {"title": m.group(1).strip("*: "), "items": []}
            out.append(current)
            continue
        if not s or current is None:
            continue
        s = re.sub(r"^[-*•]\s*", "", s)
        s = re.sub(r"\[(\d+(?:,\s*\d+)*)\]", "", s)  # [1, 2] — сноски модели
        s = re.sub(r"\s+([.,;:!?])", r"\1", s).strip()
        if s:
            current["items"].append(s)
    return out


def parse_response(data: dict[str, Any], *, model: str) -> dict[str, Any]:
    cand = (data.get("candidates") or [{}])[0]
    parts = (cand.get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
    gm = cand.get("groundingMetadata") or {}
    sources = []
    seen: set[str] = set()
    for ch in gm.get("groundingChunks") or []:
        web = ch.get("web") or {}
        uri, title = web.get("uri"), web.get("title")
        if uri and uri not in seen:
            seen.add(uri)
            sources.append({"title": title or uri, "url": uri})
    return {
        "model": model,
        "fetched_at": datetime.now(UTC).isoformat(),
        "sections": _split_sections(text),
        "text": text,
        "sources": sources,
        "queries": list(gm.get("webSearchQueries") or []),
        "grounded": bool(sources),
        "usage": data.get("usageMetadata"),
        "error": None,
        "source": SRC,
    }


def region_mode() -> str:
    return (os.environ.get("LANDCHECK_REGION") or "search").strip().lower()


def research_region(
    locality: str,
    purpose: Purpose,
    *,
    api_key: str | None = None,
    model: str = DEFAULT_MODEL,
    timeout: float = 120.0,
    fallbacks: tuple[str, ...] = FALLBACK_MODELS,
    mode: str | None = None,
    cad_num: str | None = None,
) -> dict[str, Any]:
    """Сводка по локации: бесплатный поиск + модель (search) или Gemini grounding (gemini)."""
    mode = mode or region_mode()
    if mode == "gemini" and api_key:
        return research_region_gemini(
            locality, purpose, api_key=api_key, model=model, timeout=timeout, fallbacks=fallbacks
        )
    return research_region_search(locality, purpose, cad_num=cad_num)


# --- бесплатный путь: поиск → страницы → модель ------------------------------------------
_PLACE_PREFIX = re.compile(
    r"^(городской округ|муниципальный округ|муниципальный район|город|г\.|с\.|село|пос\.|посёлок|"
    r"поселок|д\.|деревня|ст-ца|станица|рп|пгт)\s+",
    re.IGNORECASE,
)


def short_place(locality: str) -> str:
    """«Алтайский край, городской округ Белокуриха» → «Белокуриха» для поисковых запросов."""
    last = locality.split(",")[-1].strip() or locality
    return _PLACE_PREFIX.sub("", last).strip() or last


def district_of(locality: str) -> str | None:
    """«Республика Алтай, Чемальский район, с. Элекмонар» → «Чемальский район»."""
    for part in locality.split(","):
        part = part.strip()
        if re.search(r"\bрайон\b|\bр-н\b", part, re.IGNORECASE) and short_place(part) != part:
            continue
        if re.search(r"\bрайон\b|\bр-н\b", part, re.IGNORECASE):
            return part
    return None


def _stem(word: str) -> str:
    w = word.lower()
    return w[:-1] if len(w) > 5 else w


def district_stem(locality: str) -> str | None:
    """«Чемальский район» → «чемальск»: основа для поиска упоминаний района в документах."""
    d = district_of(locality)
    if not d:
        return None
    name = re.sub(r"\s*(район|р-н)\s*", " ", d, flags=re.IGNORECASE).strip()
    return name.lower()[:-2] if len(name) > 6 else name.lower()


def build_queries(locality: str, purpose: Purpose) -> list[dict[str, Any]]:
    short = short_place(locality)
    year = datetime.now(UTC).year
    queries = [
        {"q": f"{short} инвестиции проект {year}", "news": False, "recent": True},
        {"q": f"{short} строительство дорога газификация {year}", "news": False, "recent": True},
        {"q": f"{short} свалка полигон промзона кладбище", "news": False, "recent": False},
        {"q": f"{short} земельный участок цена за сотку {year}", "news": False, "recent": True},
        {"q": f"{short} генплан застройка мастер-план", "news": False, "recent": False},
        {"q": f"{short} инвестиции строительство", "news": True, "recent": True},
    ]
    district = district_of(locality)
    if district and short_place(district).lower() != short.lower():
        # Для села своих новостей мало (Элекмонар: 2 документа с одного сайта, прод 20.10) —
        # добавляем район: дороги, газ, инвестиции и генплан там решаются на уровне района.
        # Короткие запросы: «"Чемальский район" газификация» даёт 10/10 местных страниц,
        # 6 слов плюс год Яндекс размывает в Осетию и Омск (Клавдий, прод 20.10).
        queries += [
            {"q": f'"{district}" газификация', "news": False, "recent": True},
            {"q": f"{district} новости строительство", "news": False, "recent": True},
            {"q": f"{short} новости", "news": False, "recent": True},
        ]
    return queries


def collect_documents(
    locality: str, purpose: Purpose, *, search_fn=None, fetch_fn=None, max_docs: int = MAX_DOCS
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """Уникальные страницы по запросам: (документы, запросы, ошибки поиска)."""
    from landcheck import websearch

    search_fn = search_fn or websearch.search
    fetch_fn = fetch_fn or websearch.fetch_many
    seen: set[str] = set()
    per_query: list[list[dict[str, Any]]] = []
    queries: list[str] = []
    errors: list[str] = []
    short = short_place(locality).lower()
    extra = [x for x in (district_stem(locality),) if x]
    for spec in build_queries(locality, purpose):
        queries.append(spec["q"])
        res = search_fn(
            spec["q"], max_results=6, news=spec["news"], recent=spec.get("recent", False)
        )
        if res.get("error"):
            errors.append(res["error"])
            continue
        bucket: list[dict[str, Any]] = []
        for it in res["results"]:
            url = (it.get("url") or "").split("#")[0]
            title = it.get("title") or ""
            if not url or url in seen or is_junk(url, title, short) or is_classified(url, title):
                continue
            seen.add(url)
            bucket.append(
                {
                    "url": url,
                    "title": it.get("title") or url,
                    "snippet": it.get("snippet") or "",
                    "date": it.get("date"),
                    "engine": it.get("engine"),
                }
            )
        per_query.append(bucket)
    # По кругу из каждого запроса: иначе первые темы (инвестиции, стройка) занимали все
    # места, а свалки, цены за сотку и генплан до модели не доходили (прод, 20.10).
    docs: list[dict[str, Any]] = []
    for i in range(max(len(b) for b in per_query) if per_query else 0):
        docs.extend(b[i] for b in per_query if i < len(b))
    docs = docs[: max_docs * 2]
    texts = fetch_fn([d["url"] for d in docs])
    kept = []
    for d in docs:
        text = texts.get(d["url"]) or ""
        d["text"] = text if len(text) >= MIN_DOC_CHARS else d["snippet"]
        if len(d["text"]) >= MIN_SNIPPET_CHARS and is_relevant(d, short, extra):
            kept.append(d)
        if len(kept) >= max_docs:
            break
    return kept, queries, errors


_JUNK_HOSTS = (
    "google.",
    "yandex.",
    "bing.com",
    "duckduckgo.com",
    "go.mail.ru",
    "rambler.ru/search",
    # доски объявлений и соцсети: цены юнитов и реклама, а не планы территории
    "domclick.ru",
    "avito.ru",
    "cian.ru",
    "youla.ru",
    "vk.com",
    "vk.ru",
    "ok.ru",
    "t.me",
    "dzen.ru",
    "move.ru",
    "olan.ru",
    "mirkvartir.ru",
    "n1.ru",
    "etagi.com",
    "domofond.ru",
    "realty.yandex.ru",
    "realty.ya.ru",
    "irr.ru",
    "restate.ru",
    "bazarpnz.ru",
    "kupiprodai.ru",
    "zemsnab.ru",
    # туры, отели, магазины, книги: «"Чемальский район" газификация дорога» → bankturov.kz,
    # Wildberries (Клавдий, прод 20.10)
    "bankturov",
    "wildberries",
    "ozon.ru",
    "tury.ru",
    "tourister",
    "tripadvisor",
    "booking.com",
    "ostrovok",
    "101hotels",
    "sutochno",
    "litres.ru",
    "labirint.ru",
    "chitai-gorod",
    "aliexpress",
    "market.yandex",
)
# Заголовок страницы объявлений: «Купить участок в Белокурихе — 25 объявлений», «Продам участок…»
_CLASSIFIED_TITLE = re.compile(
    r"(\d+\s*объявлени|\bобъявлени[яй]\b|\bпрода[мё]|\bпродается|\bпродаётся|цен[аы]\s+от\s+\d)",
    re.IGNORECASE,
)


def is_classified(url: str, title: str) -> bool:
    """Доска объявлений: цены там — запрашиваемые, не сделки, а текст — реклама
    (ak.move.ru, olan.ru, mirkvartir.ru прошли фильтр доменов — Клавдий, прод 20.10)."""
    from urllib.parse import urlparse

    host = urlparse(url).netloc.lower()
    if any(h in host for h in _JUNK_HOSTS):
        return True
    return bool(_CLASSIFIED_TITLE.search(title or ""))


def is_relevant(
    doc: dict[str, Any], locality_short: str, extra_stems: list[str] | None = None
) -> bool:
    """Документ должен упоминать населённый пункт (или его район): иначе в сводку по Элекмонару
    попадают «Газпром: Калининградское ПХГ» и статистика по всей республике (прод, 20.10)."""
    if not locality_short:
        return True
    stems = [_stem(locality_short), *(extra_stems or [])]
    head = f"{doc.get('title') or ''} {doc.get('snippet') or ''}".lower()
    if any(st in head for st in stems):
        return True
    # В тексте — не меньше двух упоминаний: одно «в том числе в Элекмонаре» в статистике
    # по всей республике ещё не делает страницу местной. Порог 3 отрезал местные новости
    # (прокуратура, дорога к Каракольским озёрам — Клавдий, прод 20.10).
    text = (doc.get("text") or "").lower()
    return any(text.count(st) >= 2 for st in stems)


# Общие советы и гайды («Участок под глэмпинг: что нужно знать») — не про локацию
_GENERIC_TITLE = re.compile(
    r"(как выбрать|что нужно знать|совет[ыов]|гайд|гид по|топ-?\d|пошагов|что такое|"
    r"чем отличается|плюсы и минусы|стоит ли|отел[ьия]|гостиниц|\bтур[ыа]?\b|купить книгу|"
    r"интернет-магазин|бронирован)",
    re.IGNORECASE,
)


def is_junk(url: str, title: str, locality_short: str = "") -> bool:
    """Мусор из поисковой выдачи (Клавдий, прод 20.10): главные страницы google.ru, статья
    Википедии «Г», страницы поисковиков, общие гайды. Словарные статьи Википедии не про
    локацию — тоже мимо."""
    from urllib.parse import urlparse

    p = urlparse(url)
    host = p.netloc.lower()
    if any(h in host for h in _JUNK_HOSTS):
        return True
    if _GENERIC_TITLE.search(title or ""):
        return True
    if p.path in ("", "/") and not p.query:
        return True  # главная страница сайта: текста по теме там нет
    t = title.strip()
    if "wikipedia.org" in host:
        name = t.split(" — ")[0].strip()
        return len(name) <= 2 or (locality_short and locality_short not in t.lower())
    return len(t) < 3


def _render_docs(docs: list[dict[str, Any]]) -> str:
    chunks = []
    for i, d in enumerate(docs, start=1):
        head = f"[{i}] {d['title']}"
        if d.get("date"):
            head += f" ({d['date']})"
        chunks.append(f"{head}\n{d['url']}\n{d['text']}")
    return "\n\n".join(chunks)


_CAD_RE = re.compile(r"\b(\d{2}:\d{2}):\d{6,7}:\d+\b")
_REGIONAL_RE = re.compile(
    r"(республик|\bкра[йяею]\b|област[ьи]|\bрегион|\bокруг[аеу]?\b|по стране|в россии|субъект)",
    re.IGNORECASE,
)
_STATUS_RE = re.compile(
    r"(генплан|генеральн|пзз|правил[а-я]* землепользован|оопт|особо охраняем|границ|курорт|"
    r"агломерац|\bзон[аыуе]\b|статус|территориальн|населённ|населенн|черт[аеы] (города|села)|"
    r"категори|муниципал|мастер-план|проект планировки|развития территор)",
    re.IGNORECASE,
)
REGIONAL_MARK = "(по региону в целом)"
DISTRICT_MARK = "(по району в целом)"


def postprocess_sections(
    sections: list[dict[str, Any]],
    locality_short: str,
    region_name: str,
    cad_num: str | None = None,
    district_name: str | None = None,
) -> list[dict[str, Any]]:
    """Что бесплатная модель не делает по промпту — делаем кодом (Клавдий, прод 20.10):
    цифры по всей республике без пометки, участок ООПТ из чужого кадастрового района,
    «Статус территории» про стройку школы вместо генплана и ПЗЗ."""
    stem = (locality_short or "").lower()
    stem = stem[:-1] if len(stem) > 5 else stem
    region = (region_name or "").lower().strip()
    dstem = None
    if district_name:
        d_clean = re.sub(r"\s*(район|р-н)\s*", " ", district_name, flags=re.IGNORECASE).strip()
        dstem = d_clean.lower()[:-2] if len(d_clean) > 6 else d_clean.lower()
    district = (
        cad_num.split(":")[0] + ":" + cad_num.split(":")[1]
        if cad_num and cad_num.count(":") == 3
        else None
    )
    out: list[dict[str, Any]] = []
    for sec in sections:
        items: list[str] = []
        for it in sec["items"]:
            low = it.lower()
            if "данных не найдено" in low or "данные не найдены" in low:
                continue  # «Данных не найдено», «[данных не найдено]», рассуждения об отсутствии
            if sec["title"] != "Вывод для покупателя" and not re.search(r"\[\d", it):
                continue  # пункт без ссылки на документ — не обоснован
            if district and any(m != district for m in _CAD_RE.findall(it)):
                continue  # участок из другого кадастрового района — не про эту локацию
            if sec["title"] == "Статус территории" and not _STATUS_RE.search(it):
                continue
            local = bool(stem) and stem in low
            regional = _REGIONAL_RE.search(it) or (region and region in low)
            mark = None
            if not local and REGIONAL_MARK not in low and regional:
                mark = REGIONAL_MARK
            elif not local and dstem and dstem in low and DISTRICT_MARK not in low:
                mark = DISTRICT_MARK
            if mark:
                it = re.sub(r"\s*(\[[\d,\s]+\])\s*\.?\s*$", rf" {mark} \1", it, count=1)
                if mark not in it:
                    it = f"{it} {mark}"
            items.append(it)
        out.append({"title": sec["title"], "items": items or ["данных не найдено"]})
    return out


def research_region_search(
    locality: str,
    purpose: Purpose,
    *,
    search_fn=None,
    fetch_fn=None,
    chat_fn=None,
    cad_num: str | None = None,
) -> dict[str, Any]:
    from landcheck import llm

    if chat_fn is None and not llm.providers_from_env():
        return _failed(
            "search", "нет провайдера LLM: задайте бесплатный GROQ_API_KEY (или OPENROUTER_API_KEY)"
        )
    chat_fn = chat_fn or llm.chat
    docs, queries, errors = collect_documents(
        locality, purpose, search_fn=search_fn, fetch_fn=fetch_fn
    )
    if not docs:
        return _failed(
            "search",
            "по локации ничего не найдено в открытых источниках"
            + (": " + "; ".join(errors) if errors else ""),
        )
    prompt = _RAG_PROMPT.format(
        locality=locality,
        activity=purpose.activity.lower(),
        sections="\n".join(f"- {s}" for s in SECTIONS),
        docs=_render_docs(docs),
    )
    system = _RAG_SYSTEM.format(today=datetime.now(UTC).strftime("%d.%m.%Y"))
    res = chat_fn(
        [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        json_object=True,
        temperature=0.2,
        max_tokens=4000,
    )
    if res.get("error") or not res.get("json"):
        return _failed("search", res.get("error") or "модель не вернула JSON")
    data = res["json"]
    sections: list[dict[str, Any]] = []
    cited: set[int] = set()
    by_title = {
        str(s.get("title", "")).strip().lower(): s
        for s in data.get("sections") or []
        if isinstance(s, dict)
    }
    for title in SECTIONS:
        sec = by_title.get(title.lower()) or {}
        items = []
        for it in sec.get("items") or []:
            text = re.sub(r"\s+", " ", str(it)).strip()
            if text:
                items.append(text)
        sections.append({"title": title, "items": items or ["данных не найдено"]})
    sections = postprocess_sections(
        sections, short_place(locality), locality.split(",")[0], cad_num, district_of(locality)
    )
    # Ссылки считаем по итоговым пунктам: документ, на который ссылался только вырезанный
    # пункт, в источниках не нужен (Клавдий, прод 20.10).
    for sec in sections:
        for it in sec["items"]:
            for m in re.finditer(r"\[(\d+(?:\s*,\s*\d+)*)\]", it):
                cited.update(int(x) for x in re.findall(r"\d+", m.group(1)))
    if not cited:
        cited.update(n for n in data.get("used") or [] if isinstance(n, int))
    sources = [
        {"n": i, "title": d["title"], "url": d["url"], "date": d.get("date")}
        for i, d in enumerate(docs, start=1)
    ]
    grounded = bool(cited & {s["n"] for s in sources})
    if grounded:
        # В списке — только процитированные документы; номера `n` сохраняются, в шаблоне
        # <li value="n">, чтобы [4] в тексте вёл на документ 4.
        sources = [s for s in sources if s["n"] in cited]
    text = "\n".join(
        f"## {s['title']}\n" + "\n".join(f"- {i}" for i in s["items"]) for s in sections
    )
    out = {
        "model": f"{res['provider']}/{res['model']}",
        "fetched_at": datetime.now(UTC).isoformat(),
        "sections": sections if grounded else [],
        "text": text,
        "sources": sources,
        "queries": queries,
        "grounded": grounded,
        "usage": None,
        "error": None
        if grounded
        else "модель не сослалась ни на один источник — результат не показываем",
        "source": SRC,
        "search_errors": errors,
    }
    return out


# --- платный путь: Gemini с google_search (оставлен как опция) ----------------------------
def research_region_gemini(
    locality: str,
    purpose: Purpose,
    *,
    api_key: str,
    model: str = DEFAULT_MODEL,
    timeout: float = 120.0,
    fallbacks: tuple[str, ...] = FALLBACK_MODELS,
) -> dict[str, Any]:
    """Запрос к Gemini с google_search; при 503/429 пробуем запасные модели по очереди."""
    tried: list[str] = []
    result: dict[str, Any] | None = None
    for m in (model, *[f for f in fallbacks if f != model]):
        result = _research_once(locality, purpose, api_key=api_key, model=m, timeout=timeout)
        tried.append(m)
        if not result.get("retry_next"):
            break
    assert result is not None
    result.pop("retry_next", None)
    if len(tried) > 1:
        result["tried"] = tried
    return result


def _research_once(
    locality: str, purpose: Purpose, *, api_key: str, model: str, timeout: float
) -> dict[str, Any]:
    prompt = _PROMPT.format(locality=locality, activity=purpose.activity.lower())
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {"temperature": 0.2},
    }
    url = GEMINI_URL.format(model=model)
    try:
        r = httpx.post(url, params={"key": api_key}, json=body, timeout=timeout)
    except Exception as e:  # noqa: BLE001 — сеть
        return _failed(model, f"{type(e).__name__}: {e}")
    if r.status_code != 200:
        try:
            err = r.json().get("error") or {}
        except ValueError:
            err = {}
        status = err.get("status") or str(r.status_code)
        hint = {
            "RESOURCE_EXHAUSTED": "нет квоты на поиск Google у проекта Gemini: нужен платный "
            "уровень (биллинг в AI Studio)",
            "UNAVAILABLE": "модель перегружена, повторить позже",
            "NOT_FOUND": "модель недоступна этому ключу, сменить LANDCHECK_GEMINI_MODEL",
            "PERMISSION_DENIED": "ключ отклонён",
            "INVALID_ARGUMENT": "ключ или запрос отклонены",
        }.get(status, "")
        failed = _failed(model, f"Gemini {status} ({r.status_code}){': ' + hint if hint else ''}")
        failed["retry_next"] = status in ("UNAVAILABLE", "RESOURCE_EXHAUSTED", "NOT_FOUND")
        return failed
    data = r.json()
    parsed = parse_response(data, model=model)
    if not parsed["grounded"]:
        # Без ссылок ответ — это «память» модели, а не открытые источники. Не показываем.
        parsed["sections"] = []
        parsed["error"] = "модель ответила без ссылок на источники — результат не показываем"
    return parsed


def _failed(model: str, error: str) -> dict[str, Any]:
    return {
        "model": model,
        "fetched_at": datetime.now(UTC).isoformat(),
        "sections": [],
        "text": "",
        "sources": [],
        "queries": [],
        "grounded": False,
        "usage": None,
        "error": error,
        "source": SRC,
    }
