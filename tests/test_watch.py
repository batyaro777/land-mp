"""R9: слежение за участком — снимок, разница, обход и уведомление."""

from landcheck.web.watch import diff, diff_text, docs_diff, docs_snapshot, docs_text, snapshot
from tests.test_web import client, login  # noqa: F401 — фикстура клиента кабинета


def test_snapshot_and_diff():
    old = snapshot(
        {
            "found": True,
            "status": "Учтённый",
            "category": "Земли населённых пунктов",
            "permitted_use": "ИЖС",
            "specified_area_m2": 1000.0,
            "has_borders": True,
            "cadastral_cost_rub": 1_500_000.0,
            "zouit": [{"kind": "Охранная зона ЛЭП"}],
            "territorial_zones": [{"kind": "Ж-1"}],
        }
    )
    assert old["area_m2"] == 1000.0 and old["zouit"] == ["Охранная зона ЛЭП"]
    new = {**old, "status": "Архивный", "has_borders": False, "cadastral_cost_rub": 1_500_000.3}
    new["zouit"] = ["Охранная зона ЛЭП", "Санитарно-защитная зона"]
    d = diff(old, new)
    assert [c["field"] for c in d] == ["статус", "границы в ЕГРН", "ЗОУИТ"]  # стоимость: < 0,5 ₽
    assert d[0] == {"field": "статус", "old": "Учтённый", "new": "Архивный"}
    assert d[1]["old"] == "да" and d[1]["new"] == "нет"
    assert "Изменения по участку 1:2: статус: Учтённый → Архивный" in diff_text("1:2", d)
    assert diff({}, new) == []  # пустой старый снимок — сравнивать нечего


PMT = "https://adm.example.ru/docs/pmt.pdf"


def _evidence(strong: bool) -> dict:
    mention = {
        "term": "50:09:0060202:101",
        "term_kind": "cad",
        "term_label": "кадастровый номер участка",
        "page": 3,
        "quote": "…участок 50:09:0060202:101 входит в границы изъятия…",
        "url": PMT + "#page=3",
    }
    return {
        "docs": [
            {
                "url": PMT,
                "final_url": PMT,
                "title": "Проект межевания территории",
                "kind": "pmt",
                "mentions": [mention] if strong else [],
                "strong": strong,
            },
            {
                "url": "https://adm.example.ru/pzz",
                "final_url": "https://adm.example.ru/pzz",
                "title": "ПЗЗ",
                "kind": "pzz",
                "mentions": [{**mention, "url": "https://adm.example.ru/pzz"}],  # ПЗЗ — не сигнал
                "strong": False,
            },
        ],
        "hits": 2 if strong else 1,
        "checked": 2,
        "fetched_ok": 2,
        "checked_at": "2026-11-01T20:00:00+00:00",
    }


def test_docs_snapshot_diff_and_text():
    base = docs_snapshot(_evidence(False))
    assert base["strong"] == {} and base["checked"] == 2
    new = docs_snapshot(_evidence(True))
    assert list(new["strong"]) == [PMT + "#page=3"]
    added = docs_diff(base, new)
    assert added[0]["title"] == "Проект межевания территории" and added[0]["where"] == "стр. 3"
    assert docs_diff(new, new) == []
    text = docs_text("50:09:0060202:101", added)
    assert "упомянут в новом документе администрации" in text and "#page=3" in text
    assert "стр. 3" in text and "границы изъятия" in text


def test_doc_watch_baseline_then_notifies_on_new_mention(client, monkeypatch):  # noqa: F811
    from sqlalchemy import select

    from landcheck.web.db import Notification, Watch, session
    from landcheck.web.watch import run_doc_watches

    login(client)
    cad = "50:09:0060202:101"
    r = client.post("/checks", data={"numbers": cad})
    report = r.headers["location"] + f"/items/{cad}"
    client.post(f"/watch/{cad}", data={"back": report})
    calls: list[str] = []

    def fad(parcel, locality, refresh=False):
        calls.append("admin")
        return {"municipality": "тест"}

    def fev(parcel, admin_docs, locality, refresh=False):
        calls.append("evidence")
        return current[0]

    current = [_evidence(False)]
    # первая перечитка — базовый снимок, уведомлений нет
    res = run_doc_watches(max_age_h=0, fetch_admin_docs=fad, fetch_evidence=fev)
    assert res == {"checked": 1, "new": 0, "errors": []} and calls == ["admin", "evidence"]
    with session() as db:
        w = db.scalar(select(Watch))
        assert w.docs_checked_at is not None and w.docs["strong"] == {} and w.docs["new"] == []
        assert db.scalar(select(Notification)) is None
    page = client.get("/watch").text
    assert "документов 2, упоминаний участка/квартала 0" in page
    # недельный порог: свежепрочитанный участок не трогаем
    assert run_doc_watches(max_age_h=168, fetch_admin_docs=fad, fetch_evidence=fev)["checked"] == 0
    # появился проект межевания с нашим участком — уведомление со ссылкой на страницу
    current[0] = _evidence(True)
    res = run_doc_watches(max_age_h=0, fetch_admin_docs=fad, fetch_evidence=fev)
    assert res["checked"] == 1 and res["new"] == 1
    with session() as db:
        w = db.scalar(select(Watch))
        assert [n["url"] for n in w.docs["new"]] == [PMT + "#page=3"]
        n = db.scalar(select(Notification))
        assert n.kind == "watch-docs" and "Проект межевания территории" in n.text
        assert "#page=3" in n.text and n.url == report
    page = client.get("/watch").text
    assert "новое" in page and "Проект межевания территории" in page and "границы изъятия" in page
    # то же упоминание повторно — не новое
    res = run_doc_watches(max_age_h=0, fetch_admin_docs=fad, fetch_evidence=fev)
    assert res["new"] == 0
    with session() as db:
        assert db.scalar(select(Watch)).docs["new"] == []
        assert len(db.scalars(select(Notification)).all()) == 1
