"""Цели профессионала (ТЗ v3, п. 1.12): «оформить у муниципалитета», «торги / переуступка»,
«инвестиция под перепродажу» — путь по форме собственности (R21), нейтральные ВРИ и зона."""

from __future__ import annotations

from landcheck.actions import action_plan
from landcheck.intent import detect_purpose
from landcheck.models import Level, Listing, Purpose, Request
from landcheck.normalize import parcel_from_payload
from landcheck.rules import RuleContext, evaluate
from landcheck.rules.engine import BatchStats
from landcheck.web.content import checklist_for, goal_summary
from landcheck.web.serialize import result_to_dict
from tests.helpers import make_payload, zone
from tests.test_web import client, login  # noqa: F401 — фикстура клиента кабинета

AGRI = "Земли сельскохозяйственного назначения"
PUBLIC = "Собственность публично-правовых образований"


def run(payload, purpose):
    parcel = parcel_from_payload(payload)
    ctx = RuleContext(
        listing=Listing(cad_num=payload["cad_num"]),
        parcel=parcel,
        request=Request(purpose=purpose),
        stats=BatchStats(),
    )
    return parcel, {f.rule_id: f for f in evaluate(ctx)}


def test_detect_professional_purposes():
    assert detect_purpose("купить участок на торгах под ИЖС") is Purpose.AUCTION
    assert detect_purpose("переуступка аренды, аукцион в Кондопоге") is Purpose.AUCTION
    assert detect_purpose("оформить землю у администрации без торгов под КФХ") is Purpose.MUNICIPAL
    assert detect_purpose("предварительное согласование по 39.18") is Purpose.MUNICIPAL
    assert detect_purpose("инвестиция под перепродажу, до 2 млн") is Purpose.INVEST
    assert detect_purpose("магазин, торговля продуктами") is Purpose.COMMERCE  # не «торги»
    assert all(p.professional for p in (Purpose.MUNICIPAL, Purpose.AUCTION, Purpose.INVEST))
    assert not Purpose.IZHS.professional and not Purpose.MUNICIPAL.residential


def test_vri_and_zone_are_neutral_for_professional_goals():
    payload = make_payload(
        "40:10:160500:1",
        category=AGRI,
        vri="Для сельскохозяйственного производства",
        ownership=PUBLIC,
        tz=[zone("Территориальные зоны", "1", "Сх-1")],
    )
    for purpose in (Purpose.MUNICIPAL, Purpose.AUCTION):
        _, flags = run(payload, purpose)
        assert "R05" not in flags and "R04" not in flags, purpose
        assert flags["R11"].level is Level.OK and "R21" in flags
    # для сравнения: под ИЖС та же сельхозка — стоп по категории
    _, flags = run(payload, Purpose.IZHS)
    assert flags["R04"].level is Level.STOP


def test_r21_municipal_by_ownership():
    _, flags = run(make_payload("x", ownership="Частная собственность"), Purpose.MUNICIPAL)
    assert flags["R21"].level is Level.STOP and "частной собственности" in flags["R21"].message
    _, flags = run(make_payload("x", ownership=PUBLIC), Purpose.MUNICIPAL)
    f = flags["R21"]
    assert f.level is Level.CHECK and "39.15" in f.message and "39.18" in f.message
    assert "101-ФЗ" in f.message and f.details["purpose"] == "municipal"
    _, flags = run(make_payload("x", ownership=None), Purpose.MUNICIPAL)
    assert flags["R21"].level is Level.CHECK and "не указана" in flags["R21"].message


def test_r21_auction_and_invest():
    _, flags = run(make_payload("x", ownership=PUBLIC), Purpose.AUCTION)
    assert flags["R21"].level is Level.CHECK and "ст. 22 ЗК" in flags["R21"].message
    assert "39.11" in flags["R21"].message
    _, flags = run(make_payload("x", ownership="Частная собственность"), Purpose.AUCTION)
    assert flags["R21"].level is Level.CHECK and "банкротств" in flags["R21"].message
    _, flags = run(make_payload("x", ownership="Частная собственность"), Purpose.INVEST)
    assert flags["R21"].level is Level.OK and "217.1" in flags["R21"].message
    _, flags = run(make_payload("x", ownership=PUBLIC), Purpose.INVEST)
    assert flags["R21"].level is Level.CHECK and "переуступ" in flags["R21"].message
    # сельхозка под инвестицию: преимущественное право региона (ст. 8 101-ФЗ) — проверить
    _, flags = run(
        make_payload("x", category=AGRI, vri="Для сельскохозяйственного производства"),
        Purpose.INVEST,
    )
    assert flags["R04"].level is Level.CHECK and "преимущественное право" in flags["R04"].message


def test_goal_summary_plan_and_checklist_for_professional():
    from landcheck.models import CheckResult

    payload = make_payload("50:09:0060202:101", ownership=PUBLIC)
    parcel, flags = run(payload, Purpose.AUCTION)
    item = result_to_dict(
        CheckResult(
            listing=Listing(cad_num=payload["cad_num"]), parcel=parcel, flags=list(flags.values())
        )
    )
    goal = goal_summary(item, Purpose.AUCTION)
    assert goal["question"] == "Как получить" and goal["professional"]
    assert [r["rule_id"] for r in goal["rows"]] == ["R21", "R04"]
    assert goal["rows"][0]["value"] == PUBLIC and goal["level"] == "проверить"
    assert "торгов" in goal["text"]
    plan = action_plan(item)
    r21 = next(s for s in plan["steps"] if s["rule_id"] == "R21")
    texts = " ".join(a["what"] + a["where"] for a in r21["actions"])
    assert "torgi.gov.ru" in texts and "калькулятор" in texts.lower() and "задаток" in texts
    # «у муниципалитета» — другой план
    parcel, flags = run(payload, Purpose.MUNICIPAL)
    item = result_to_dict(
        CheckResult(
            listing=Listing(cad_num=payload["cad_num"]), parcel=parcel, flags=list(flags.values())
        )
    )
    plan = action_plan(item)
    r21 = next(s for s in plan["steps"] if s["rule_id"] == "R21")
    assert any("39.15" in a["what"] for a in r21["actions"])
    assert goal_summary(item, Purpose.MUNICIPAL)["rows"][0]["title"] == "Форма собственности и путь"
    # чек-лист инвестора: блок экономики перепродажи
    private = result_to_dict(
        CheckResult(
            listing=Listing(cad_num="x"),
            parcel=parcel_from_payload(make_payload("x", ownership="Частная собственность")),
            flags=[],
        )
    )
    cl = checklist_for(private, Purpose.INVEST)
    assert any(b["title"] == "Экономика перепродажи" for b in cl["blocks"])
    # и у муниципального участка тоже (приёмка 00a6150: блок терялся на раннем return)
    public_item = result_to_dict(
        CheckResult(listing=Listing(cad_num="x"), parcel=parcel_from_payload(payload), flags=[])
    )
    cl = checklist_for(public_item, Purpose.INVEST)
    assert cl["title"] == "Чек-лист перед оформлением участка"
    assert any(b["title"] == "Экономика перепродажи" for b in cl["blocks"])
    assert not any(
        b["title"] == "Экономика перепродажи"
        for b in checklist_for(private, Purpose.IZHS)["blocks"]
    )


def test_professional_goals_in_dropdown_and_check(client):  # noqa: F811
    login(client)
    page = client.get("/").text
    for p in (Purpose.MUNICIPAL, Purpose.AUCTION, Purpose.INVEST):  # список целей — словами
        assert f'value="{p.value}"' in page and p.activity in page
    r = client.post(
        "/checks", data={"numbers": "50:09:0060202:101; 2 500 000; 10 сот", "purpose": "auction"}
    )
    assert r.status_code == 303
    report = client.get(r.headers["location"] + "/items/50:09:0060202:101").text
    assert "Как получить: купить или арендовать на торгах" in report
    assert "Форма собственности и путь" in report and "Можно ли здесь" not in report
