"""Первоисточники до страницы (ТЗ v3, п. 2.5, этап 2): цитаты из документов администрации,
флаг R25 и ссылки на статьи законов у флагов."""

from __future__ import annotations

import time

from landcheck import evidence
from landcheck.lawlinks import law_links
from tests.test_web import client, login  # noqa: F401 — фикстура клиента кабинета

PARCEL = {
    "cad_num": "40:10:160500:1",
    "address": "Калужская область, Козельский район, д. Дешовки",
}
PMT = "https://adm-kozelsk.ru/docs/pmt-deshovki.pdf"
NPA = "https://adm-kozelsk.ru/npa/207"
GISOGD = "https://gisogd.admoblkaluga.ru/doc/1"
ADMIN_DOCS = {  # сводка блока «Документы администрации» с определённым муниципалитетом
    "municipality": "Козельский район",
    "region": "Калужская область",
    "registry": None,
    "registry_checked": False,
    "regional": [],
    "federal": [],
    "found": {"docs": {}, "total": 0, "dropped": 0, "errors": [], "engine": None},
    "kind_titles": {},
    "checklist": [],
    "source": "тест",
    "note": "",
}


def _doc(url: str, pages: list[str], *, pdf: bool = True, title: str = "Документ") -> dict:
    return {
        "url": url,
        "final_url": url,
        "title": title,
        "content_type": "application/pdf" if pdf else "text/html",
        "is_pdf": pdf,
        "pages": [{"n": (i + 1) if pdf else None, "text": t} for i, t in enumerate(pages)],
        "error": None,
    }


def test_terms_cover_parcel_quarter_and_locality():
    terms = evidence.terms_for(PARCEL, "Калужская область, Козельский район, д. Дешовки")
    assert [t["value"] for t in terms] == ["40:10:160500:1", "40:10:160500", "Дешовки"]
    assert evidence.quarter_of("40:10:160500:1") == "40:10:160500"
    assert evidence.quarter_of("мусор") is None


def test_find_mentions_tolerates_spaces_and_points_to_page():
    doc = _doc(
        PMT,
        [
            "Титульный лист проекта межевания территории",
            "",
            "3. Перечень образуемых участков: 40:10 : 160500 : 1 — под улицу; 40:10:160500:7.",
            "Пояснительная записка. Деревня Дешовки, Козельский район.",
        ],
    )
    terms = evidence.terms_for(PARCEL, "д. Дешовки")
    found = evidence.find_mentions(doc, terms)
    kinds = {(m["term_kind"], m["page"]) for m in found}
    assert ("cad", 3) in kinds and ("quarter", 3) in kinds and ("locality", 4) in kinds
    cad = next(m for m in found if m["term_kind"] == "cad")
    assert "40:10 : 160500 : 1" in cad["quote"] and cad["term"] == "40:10:160500:1"
    assert evidence.page_url(PMT, 3) == PMT + "#page=3"
    assert evidence.page_url(NPA, None) == NPA


def test_candidate_docs_registry_first_then_kinds_without_gisogd():
    admin_docs = {
        "registry": {"links": [{"url": NPA, "title": "НПА администрации"}]},
        "found": {
            "docs": {
                "gisogd": [{"url": GISOGD, "title": "ГИСОГД"}],
                "pzz": [{"url": "https://adm-kozelsk.ru/pzz", "title": "ПЗЗ"}],
                "pmt": [{"url": PMT, "title": "ПМТ"}, {"url": NPA, "title": "дубль"}],
            }
        },
    }
    cands = evidence.candidate_docs(admin_docs)
    assert [c["kind"] for c in cands] == ["registry", "pmt", "pzz"]
    assert cands[0]["url"] == NPA and cands[1]["url"] == PMT
    assert evidence.candidate_docs(admin_docs, max_docs=1) == cands[:1]
    assert evidence.candidate_docs(None) == []


def test_collect_counts_hits_and_r25_points_to_page():
    admin_docs = {
        "registry": {"links": [{"url": NPA, "title": "НПА"}]},
        "found": {"docs": {"pmt": [{"url": PMT, "title": "ПМТ Дешовки"}]}},
    }
    docs = {
        NPA: _doc(NPA, ["Постановления администрации за 2026 год. Дешовки."], pdf=False),
        PMT: _doc(PMT, ["ПМТ", "", "Участок 40:10:160500:1 изымается под дорогу"]),
    }
    broken = {"url": "x", "final_url": "x", "title": None, "pages": [], "error": "HTTP 404"}
    calls: list[str] = []

    def fetch(url: str) -> dict:
        calls.append(url)
        return docs.get(url, broken)

    ev = evidence.collect(PARCEL, admin_docs, "д. Дешовки", fetch=fetch)
    assert sorted(calls) == sorted([NPA, PMT])  # параллельно, порядок вызовов не фиксирован
    # квартал внутри полного номера отдельно не считается — только сам участок и деревня
    assert ev["checked"] == 2 and ev["fetched_ok"] == 2 and ev["hits"] == 2
    pmt = next(d for d in ev["docs"] if d["url"] == PMT)
    assert pmt["strong"] and pmt["pages"] == 3 and pmt["is_pdf"]
    assert pmt["mentions"][0]["url"] == PMT + "#page=3"
    npa = next(d for d in ev["docs"] if d["url"] == NPA)
    assert not npa["strong"] and npa["mentions"][0]["term_kind"] == "locality"
    assert npa["mentions"][0]["url"] == NPA  # HTML — без #page

    flag = evidence.r25_flag(ev)
    assert flag and flag["rule_id"] == "R25" and flag["level"] == "проверить"
    assert flag["source"] == PMT + "#page=3"
    assert "40:10:160500:1" in flag["message"] and "стр. 3" in flag["message"]
    assert "изымается под дорогу" in flag["details"]["quote"]

    # только населённый пункт — не повод для флага
    ev2 = evidence.collect(
        PARCEL, {"registry": {"links": [{"url": NPA}]}}, "д. Дешовки", fetch=fetch
    )
    assert ev2["hits"] == 1 and ev2["strong"] == 0 and evidence.r25_flag(ev2) is None
    # документ не прочитан — честно помечен
    ev3 = evidence.collect(PARCEL, {"registry": {"links": [{"url": "x"}]}}, None, fetch=fetch)
    assert ev3["fetched_ok"] == 0 and ev3["docs"][0]["error"] == "HTTP 404"


def test_law_links_link_codes_and_urls_and_escape_html():
    html = str(law_links("ст. 39.18 ЗК РФ; 33-ФЗ; <b>x</b>; https://adm.ru/npa/2#page=3 (НСПД)"))
    assert 'href="https://www.consultant.ru/document/cons_doc_LAW_33773/"' in html
    assert ">ЗК РФ</a>" in html and 'title="Земельный кодекс РФ"' in html
    assert 'href="https://www.consultant.ru/document/cons_doc_LAW_6072/"' in html
    assert "&lt;b&gt;x&lt;/b&gt;" in html and "<b>" not in html
    assert '<a href="https://adm.ru/npa/2#page=3" target="_blank" rel="noopener">' in html
    assert html.endswith(" (НСПД)")
    assert str(law_links(None)) == "" and str(law_links("ЗКН, УЗК")) == "ЗКН, УЗК"
    assert "consultant" not in str(law_links("1033-ФЗ"))  # не часть другого номера


def test_evidence_route_pending_then_ready_with_r25(client, monkeypatch):  # noqa: F811
    from sqlalchemy import select

    from landcheck.web import jobs
    from landcheck.web.db import CheckItem, session

    canned = {
        "terms": [{"kind": "cad", "value": "50:09:0060202:102", "label": "кадастровый номер"}],
        "docs": [
            {
                "url": PMT,
                "final_url": PMT,
                "title": "Проект межевания территории д. Дешовки",
                "kind": "pmt",
                "is_pdf": True,
                "pages": 12,
                "error": None,
                "mentions": [
                    {
                        "term": "50:09:0060202:102",
                        "term_kind": "cad",
                        "term_label": "кадастровый номер участка",
                        "page": 7,
                        "quote": "…участок 50:09:0060202:102 входит в границы изъятия…",
                        "url": PMT + "#page=7",
                    }
                ],
                "strong": True,
            },
            {
                "url": NPA,
                "final_url": NPA,
                "title": "НПА",
                "kind": "registry",
                "is_pdf": False,
                "pages": 0,
                "error": "HTTP 403",
                "mentions": [],
                "strong": False,
            },
        ],
        "hits": 1,
        "strong": 1,
        "checked": 2,
        "fetched_ok": 1,
        "checked_at": "2026-11-01T18:00:00+00:00",
        "note": "Проверены текстовые слои документов.",
    }
    calls: list[bool] = []

    def fake_fetch(parcel, admin_docs, locality, *, refresh=False):
        calls.append(refresh)
        return canned

    monkeypatch.setattr(jobs, "fetch_evidence", fake_fetch)
    monkeypatch.setattr(
        jobs, "fetch_admin_docs", lambda parcel, locality, refresh=False: ADMIN_DOCS
    )
    monkeypatch.setattr(
        jobs,
        "start_evidence",
        lambda item_id, cad, refresh=False: (jobs._evidence_job(item_id, cad, refresh), True)[1],
    )
    login(client)
    r = client.post("/checks", data={"numbers": "50:09:0060202:102", "purpose": "izhs"})
    base = r.headers["location"] + "/items/50:09:0060202:102"
    block = client.get(base + "/evidence").text  # первый заход — заглушка и опрос
    assert "Упоминания участка в документах администрации" in block
    assert 'hx-get="' + base + '/evidence"' in block and "every 20s" in block
    assert calls == [False]
    block = client.get(base + "/evidence").text  # фон записал результат
    assert (
        "every 20s" not in block and "проверено документов: 2, прочитано: 1, упоминаний: 1" in block
    )
    assert f'href="{PMT}#page=7"' in block and "стр. 7" in block and "границы изъятия" in block
    assert "PDF, 12 стр." in block and "HTTP 403" in block
    assert len(calls) == 1
    with session() as db:
        item = db.scalar(select(CheckItem))
        flags = {f["rule_id"]: f for f in item.data["flags"]}
        assert flags["R25"]["source"] == PMT + "#page=7"
        assert item.data["verdict"] == "стоп"  # сельхоз под ИЖС — стоп по R04, R25 его не снижает
        assert item.data["admin_docs"] == ADMIN_DOCS
    page = client.get(base).text  # ссылка на страницу первоисточника — у флага в отчёте
    assert f'<a href="{PMT}#page=7" target="_blank" rel="noopener">' in page
    # сельхоз-участок: у R04 источник «ст. 7 ЗК РФ» — в отчёте это ссылка на текст кодекса
    assert 'href="https://www.consultant.ru/document/cons_doc_LAW_33773/"' in page
    client.get(base + "/evidence?refresh=1")
    assert calls == [False, True]
    with session() as db:  # повтор не плодит R25
        item = db.scalar(select(CheckItem))
        assert [f["rule_id"] for f in item.data["flags"]].count("R25") == 1


def test_admin_docs_block_embeds_evidence_slot(client, monkeypatch):  # noqa: F811
    from sqlalchemy import select

    from landcheck.web.db import CheckItem, session

    login(client)
    r = client.post("/checks", data={"numbers": "50:09:0060202:101", "purpose": "izhs"})
    base = r.headers["location"] + "/items/50:09:0060202:101"
    with session() as db:
        item = db.scalar(select(CheckItem))
        item.data = {**item.data, "admin_docs": ADMIN_DOCS}
        db.commit()
    block = client.get(base + "/admin-docs").text
    assert 'id="evidence-slot" hx-get="' + base + '/evidence"' in block
    assert 'hx-trigger="load delay:2s"' in block


def test_collect_marks_documents_that_miss_the_time_budget():
    import threading

    gate = threading.Event()

    def slow_fetch(url: str) -> dict:
        if url == PMT:
            gate.wait(2)  # дольше бюджета проверки
            return _doc(PMT, ["Участок 40:10:160500:1"])
        return _doc(NPA, ["Дешовки"], pdf=False)

    admin_docs = {
        "registry": {"links": [{"url": NPA}]},
        "found": {"docs": {"pmt": [{"url": PMT}]}},
    }
    ev = evidence.collect(PARCEL, admin_docs, "д. Дешовки", fetch=slow_fetch, budget_s=0.3)
    gate.set()
    by_url = {d["url"]: d for d in ev["docs"]}
    assert ev["timed_out"] == 1 and "лимит времени" in by_url[PMT]["error"]
    assert by_url[NPA]["mentions"] and ev["fetched_ok"] == 1 and "Не успели" in ev["note"]
    assert evidence.r25_flag(ev) is None


def test_fetch_document_reports_download_errors(monkeypatch):
    def boom(url):
        raise ConnectionError("ConnectTimeout; ConnectTimeout через шлюз")

    monkeypatch.setattr(evidence, "_download", boom)
    doc = evidence.fetch_document("https://adm.example.ru/x.pdf")
    assert doc["pages"] == [] and doc["error"].startswith("не скачан: ConnectTimeout")


def test_decode_html_honours_declared_charset_and_falls_back_to_cp1251():
    text = "Постановление администрации «О проекте»"
    assert evidence.decode_html(text.encode("cp1251"), "text/html; charset=windows-1251") == text
    meta = ('<html><head><meta charset="windows-1251"><title>Док</title></head>' + text).encode(
        "cp1251"
    )
    assert text in evidence.decode_html(meta, "text/html")
    assert text in evidence.decode_html(text.encode("cp1251"), "text/html")  # без объявления
    assert evidence.decode_html(text.encode("utf-8"), "text/html") == text
    assert evidence._html_title("<title>Проект &quot;Север&quot; &mdash; ПМТ</title>") == (
        'Проект "Север" — ПМТ'
    )


def test_page_links_and_nested_candidates_pick_relevant_pdfs():
    html = (
        '<a href="/docs/pmt-deshovki.pdf">Проект межевания д. Дешовки</a>'
        '<a href="https://adm-kozelsk.ru/docs/budget-2026.pdf">Бюджет 2026</a>'
        '<a href="/docs/postanovlenie-207.PDF?x=1">Постановление № 207</a>'
        '<a href="/docs/pmt-deshovki.pdf">дубль</a>'
        '<a href="/news/1">Новости</a>'
        '<a href="mailto:a@b.ru">почта</a>'
    )
    links = evidence.page_links(html, NPA)
    assert [link["url"] for link in links] == [
        "https://adm-kozelsk.ru/docs/pmt-deshovki.pdf",
        "https://adm-kozelsk.ru/docs/budget-2026.pdf",
        "https://adm-kozelsk.ru/docs/postanovlenie-207.PDF?x=1",
    ]
    terms = evidence.terms_for(PARCEL, "д. Дешовки")
    cands = [{"url": NPA, "title": "НПА", "kind": "npa"}, {"url": GISOGD, "kind": "pzz"}]
    docs = [
        {"final_url": NPA, "links": links, "pages": [{"n": None, "text": "x"}]},
        {"links": links},
    ]
    nested = evidence.nested_candidates(cands, docs, terms)
    assert [n["url"] for n in nested] == [
        "https://adm-kozelsk.ru/docs/pmt-deshovki.pdf",  # межевание + Дешовки
        "https://adm-kozelsk.ru/docs/postanovlenie-207.PDF?x=1",  # постановление
    ]
    assert nested[0]["kind"] == "npa" and nested[0]["via"] == NPA
    assert nested[0]["title"] == "Проект межевания д. Дешовки"  # ПЗЗ-страницы вторую волну не дают


def test_collect_reads_nested_pdf_and_raises_r25():
    nested_pdf = "https://adm-kozelsk.ru/docs/pmt-deshovki.pdf"
    index = _doc(NPA, ["Раздел «Планировка территории»"], pdf=False, title="Планировка")
    index["links"] = [{"url": nested_pdf, "text": "Проект межевания д. Дешовки"}]
    docs = {
        NPA: index,
        nested_pdf: _doc(nested_pdf, ["ПМТ", "Образуемый участок 40:10:160500:1"], title="ПМТ"),
    }
    calls: list[str] = []

    def fetch(url: str) -> dict:
        calls.append(url)
        return docs[url]

    admin_docs = {"registry": {"links": [{"url": NPA, "title": "Планировка"}]}}
    ev = evidence.collect(PARCEL, admin_docs, "д. Дешовки", fetch=fetch)
    assert calls == [NPA, nested_pdf] and ev["checked"] == 2 and ev["nested"] == 1
    pdf = ev["docs"][1]
    assert (
        pdf["via"] == NPA and pdf["strong"] and pdf["mentions"][0]["url"] == nested_pdf + "#page=2"
    )
    flag = evidence.r25_flag(ev)
    assert flag and flag["source"] == nested_pdf + "#page=2"
    # бюджет почти исчерпан — вторая волна честно пропускается
    ev2 = evidence.collect(PARCEL, admin_docs, "д. Дешовки", fetch=fetch, budget_s=0.0)
    assert ev2["nested"] == 0 and "Вложенные файлы разделов (1) не проверены" in ev2["note"]


def test_explain_download_errors_in_plain_words():
    import httpx

    req = httpx.Request("GET", "https://x.ru")
    assert evidence._explain(httpx.ConnectTimeout("t", request=req)) == (
        "сайт не отвечает (таймаут соединения)"
    )
    assert evidence._explain(httpx.ConnectError("[Errno -2] Name or service not known")) == (
        "адрес сайта не найден (DNS)"
    )
    assert evidence._explain(httpx.RemoteProtocolError("Malformed reply")) == (
        "сайт оборвал соединение"
    )
    assert evidence.doc_key("https://x.ru/a").startswith("doc2:")

    class ProtocolError(Exception):  # как socksio.exceptions.ProtocolError
        pass

    ProtocolError.__module__ = "socksio.exceptions"
    assert evidence._explain(ProtocolError("Malformed reply")) == (
        "шлюз не получил ответ от сайта (обрыв SOCKS)"
    )


def test_download_explains_unknown_transport_errors(monkeypatch):
    import httpx

    class ProtocolError(Exception):
        pass

    ProtocolError.__module__ = "socksio.exceptions"
    calls: list[str | None] = []

    class FakeClient:
        def __init__(self, *, proxy=None, **kw):
            calls.append(proxy)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def stream(self, method, url):
            raise ProtocolError("Malformed reply")

    monkeypatch.setattr(httpx, "Client", FakeClient)
    monkeypatch.setattr(evidence.ruproxy, "enabled", lambda: True)
    monkeypatch.setattr(evidence.ruproxy, "proxy_url", lambda: "socks5h://gw:1080")
    monkeypatch.setattr(evidence.ruproxy, "verify_context", lambda: True)
    doc = evidence.fetch_document("https://gosweb.example.ru/x")
    assert calls == [None, "socks5h://gw:1080"]
    assert doc["error"] == (
        "не скачан: шлюз не получил ответ от сайта (обрыв SOCKS); "
        "шлюз не получил ответ от сайта (обрыв SOCKS)"  # без дубля «через шлюз»
    )


def test_download_attempt_is_limited_by_wall_clock(monkeypatch):
    import threading

    import httpx

    gate = threading.Event()

    class HangingClient:
        def __init__(self, *, proxy=None, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def stream(self, method, url):
            gate.wait(5)  # «рукопожатие» с шлюзом, которое таймауты httpx не ограничивают
            raise RuntimeError("never")

    monkeypatch.setattr(httpx, "Client", HangingClient)
    monkeypatch.setattr(evidence.ruproxy, "enabled", lambda: False)
    monkeypatch.setattr(evidence, "ATTEMPT_DEADLINE_S", 0.2)
    t0 = time.monotonic()
    doc = evidence.fetch_document("https://slow.example.ru/x")
    gate.set()
    assert time.monotonic() - t0 < 3
    assert doc["error"] == "не скачан: нет ответа за 0 с"


def test_page_links_strip_file_size_from_anchor():
    html = (
        '<a href="/f/a.pdf"><span>55.19 КБ</span> Решение ГСС от 10.04.2025 № 44</a>'
        '<a href="/f/b.pdf">Проект планировки (4.3 МБ)</a>'
    )
    links = evidence.page_links(html, "https://sochi.ru/x/")
    assert [link["text"] for link in links] == [
        "Решение ГСС от 10.04.2025 № 44",
        "Проект планировки",
    ]
