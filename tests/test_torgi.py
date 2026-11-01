"""Торги рядом (ТЗ v3, п. 9): разбор API torgi.gov.ru, фильтр по району, статистика, ошибки."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from landcheck import ruproxy, torgi

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def _card(
    lot_id: str,
    cad: str,
    *,
    area: float = 1000.0,
    start: float | None = 100_000,
    fin: float | None = 150_000,
    kind: str = "sale",
    status: str = "SUCCEED",
    form: str = "EA",
    vri: str = "Для индивидуального жилищного строительства",
    date: str = "2026-10-28T09:00:00Z",
) -> dict:
    chars = [
        {"code": "CadastralNumber", "name": "Кадастровый номер", "characteristicValue": cad},
        {"code": "SquareZU", "name": "Площадь", "characteristicValue": area},
        {
            "code": "PermittedUse",
            "characteristicValue": [{"code": "2.1", "name": vri, "selectNsi": "PermittedUse"}],
        },
    ]
    card = {
        "id": lot_id,
        "lotName": f"продажа участка {cad}",
        "lotDescription": "",
        "lotStatus": status,
        "biddForm": {"code": form, "name": "Электронный аукцион"},
        "biddType": {"code": "ZK", "name": "Аренда и продажа земельных участков"},
        "subjectRFCode": cad.split(":")[0],
        "characteristics": chars,
        "category": {"code": "4", "name": "Земли сельскохозяйственного назначения"},
        "estateAddress": "Калужская обл., Малоярославецкий р-н",
        "typeTransaction": kind,
        "auctionStartDate": date,
        "noticeFirstVersionPublicationDate": "2026-10-01T11:16:26.79Z",
    }
    if start is not None:
        card["priceMin"] = start
    if fin is not None:
        card["priceFin"] = fin
    return card


SUCCEED = [
    _card("A_1", "40:13:120101:22", area=2000, start=100_000, fin=150_000),  # 75 ₽/м²
    _card("A_2", "40:13:020705:7", area=1000, start=50_000, fin=50_000, date="2026-09-01"),  # 50
    _card("A_3", "40:13:000001:1", area=500, start=10_000, fin=20_000, kind="rent"),  # 40/м²·год
    _card("B_1", "40:04:010214:114", area=1000, start=1, fin=999_999),  # чужой район по тексту
]


FAILED = [
    _card("F_1", "40:13:000002:1", fin=None, status="FAILED"),
    _card("F_2", "40:13:000002:2", fin=None, status="FAILED"),
    _card("F_9", "40:17:000002:9", fin=None, status="FAILED"),
]
ACTIVE = [_card("P_1", "40:13:000003:1", fin=None, status="APPLICATIONS_SUBMISSION")]


def _page(content: list[dict], total: int) -> dict:
    return {
        "content": content,
        "totalElements": total,
        "totalPages": (total + 9) // 10,
        "size": 10,
        "number": 0,
    }


def _handler(seen: list[dict]):
    def handler(req: httpx.Request) -> httpx.Response:
        url = urlparse(str(req.url))
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        seen.append({"path": url.path, **q})
        if url.path.endswith("/lotcards/search"):
            assert q["catCode"] == "2" and q["subjRF"] == "40" and q["biddForm"] == "EA"
            assert q["text"] == "40:13" and q["pubFrom"] == "2025-11-06"
            status = q["lotStatus"]
            if status == "SUCCEED":
                # в поиске итоговой цены нет — только старт
                hits = [{k: v for k, v in c.items() if k != "priceFin"} for c in SUCCEED]
                return httpx.Response(200, json=_page(hits, 4))
            if status == "FAILED":  # два своих, один чужой район — считаем только свои
                return httpx.Response(200, json=_page(FAILED, 3))
            return httpx.Response(200, json=_page(ACTIVE, 1))  # идут сейчас
        lot_id = url.path.rsplit("/", 1)[-1]
        card = next(c for c in SUCCEED if c["id"] == lot_id)
        return httpx.Response(200, json=card)

    return handler


def test_nearby_auctions_stats_and_district_filter():
    seen: list[dict] = []
    client = httpx.Client(transport=httpx.MockTransport(_handler(seen)))
    a = torgi.nearby_auctions("40:13:120101:22", client=client, now=NOW, interval=0)
    assert a["error"] is None and a["district"] == "40:13" and a["region"] == "40"
    # счётчики — только по лотам своего района, а не по totalElements поиска (он шире)
    assert (a["succeed_total"], a["failed_total"], a["active_total"], a["ea_total"]) == (
        3,
        2,
        1,
        6,
    )
    assert a["liquidity"] == 0.6 and a["complete"] is True and a["requests"] == 6
    assert a["retries"] == 0
    # карточки тянем только для лотов своего района: 3 из 4
    cards = [s for s in seen if "/lotcards/" in s["path"] and not s["path"].endswith("search")]
    assert len(cards) == 3 and all(not s["path"].endswith("B_1") for s in cards)
    assert [i["cad_num"] for i in a["items"]] == [
        "40:13:120101:22",
        "40:13:000001:1",
        "40:13:020705:7",
    ]  # по дате, новые сверху
    assert a["sale"] == {
        "count": 2,
        "with_price": 2,
        "median_rub_m2": 62.5,
        "p25_rub_m2": 56.2,
        "p75_rub_m2": 68.8,
        "median_ratio": 1.25,
    }
    assert a["rent"]["count"] == 1 and a["rent"]["median_rub_m2"] == 40.0
    assert a["items"][0]["url"].endswith("/lots/lot/A_1") and a["items"][0]["kind"] == "продажа"
    assert "состоялись 3 из 5 завершённых (60 %)" in a["liquidity_text"]
    assert "спрос есть" in a["liquidity_text"]
    assert a["sample_note"] is None
    json.dumps(a, ensure_ascii=False)  # сериализуется в item.data


def test_nearby_auctions_marks_truncated_pages():
    def handler(req: httpx.Request) -> httpx.Response:
        url = urlparse(str(req.url))
        if url.path.endswith("/lotcards/search"):
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            page = int(q["page"])
            hit = _card(f"S_{q['lotStatus'][:3]}_{page}", f"40:13:0000{page:02d}:1", fin=None)
            return httpx.Response(200, json={**_page([hit], 500), "totalPages": 50})
        return httpx.Response(200, json=_card(url.path.rsplit("/", 1)[-1], "40:13:000001:1"))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    a = torgi.nearby_auctions("40:13:120101:22", client=client, now=NOW, interval=0)
    assert a["complete"] is False and a["succeed_total"] == torgi.MAX_PAGES
    assert "счётчики по району занижены" in a["sample_note"]
    assert a["requests"] == torgi.MAX_PAGES * 3 + min(torgi.MAX_PAGES, torgi.MAX_CARDS)


def test_throttle_and_503_retry():
    """torgi режет частые запросы: пауза между запросами и повтор на 503 с выдержкой."""
    torgi._last_request_at = 0.0
    calls = {"search": 0}
    sleeps: list[float] = []

    def handler(req: httpx.Request) -> httpx.Response:
        url = urlparse(str(req.url))
        if url.path.endswith("/lotcards/search"):
            calls["search"] += 1
            if calls["search"] == 1:
                return httpx.Response(503, text="Service Unavailable")
            return httpx.Response(200, json=_page([], 0))
        return httpx.Response(200, json={})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    a = torgi.nearby_auctions(
        "40:13:120101:22", client=client, now=NOW, interval=6.0, sleep=sleeps.append
    )
    assert a["error"] is None and a["retries"] == 1 and a["requests"] == 4
    assert torgi.RETRY_DELAYS_S[0] in sleeps  # выдержка после 503
    gaps = [x for x in sleeps if x not in torgi.RETRY_DELAYS_S]
    assert len(gaps) >= 3 and all(0 < g <= 6.0 for g in gaps)  # пауза между запросами


def test_nearby_auctions_network_error_is_contained():
    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("socks refused", request=req)

    client = httpx.Client(transport=httpx.MockTransport(boom))
    a = torgi.nearby_auctions("40:13:120101:22", client=client, now=NOW, interval=0)
    assert a["error"] == "torgi.gov.ru не ответил: ConnectError"
    assert a["liquidity_text"] == a["error"] and a["items"] == [] and a["ea_total"] == 0


def test_nearby_auctions_without_gateway(monkeypatch):
    monkeypatch.delenv("LANDCHECK_RU_PROXY", raising=False)
    assert not ruproxy.enabled()
    a = torgi.nearby_auctions("40:13:120101:22", now=NOW)
    assert "RU-шлюз не настроен" in a["error"]
    assert torgi.nearby_auctions("плохой номер", now=NOW)["error"].startswith("кадастровый номер")


def test_card_parsing_and_edge_cases():
    assert torgi.district_of("40:13:120101:22") == "40:13"
    assert torgi.district_of("4:5:1:1") == "04:05" and torgi.district_of("abc") is None
    assert torgi.region_of(None) is None
    # «сообщение о предоставлении»: цены нет, площадь — из характеристик, номер — из описания
    ips = _card("I_1", "40:13:1:1", start=None, fin=None, form="IPS", kind="rent")
    ips["characteristics"] = [{"code": "SquareZU", "characteristicValue": 700.0}]
    ips["lotDescription"] = "участок с кадастровым номером 40:13:020705:1868 площадью 700 кв.м"
    row = torgi.item_of(ips, "40:13")
    assert row and row["cad_num"] == "40:13:020705:1868" and row["kind"] == "аренда"
    assert row["price_fin"] is None and row["rub_m2"] is None and row["ratio"] is None
    assert torgi.item_of(ips, "40:04") is None
    tiny = _card("T_1", "40:13:000001:2", area=10)  # 10 м² — не участок
    assert torgi.item_of(tiny, "40:13")["area_m2"] is None


def test_liquidity_text_thresholds():
    base = torgi.empty_result("40:13:1:1")
    assert "не было" in torgi.liquidity_text(base)
    strong = {**base, "ea_total": 10, "succeed_total": 7, "failed_total": 3, "active_total": 0}
    strong["liquidity"] = 0.7
    assert "спрос есть" in torgi.liquidity_text(strong)
    weak = {**strong, "succeed_total": 1, "failed_total": 9, "liquidity": 0.1}
    assert "спрос слабый" in torgi.liquidity_text(weak)
    only_active = {**base, "ea_total": 2, "active_total": 2, "liquidity": None}
    assert "завершённых за год ещё нет" in torgi.liquidity_text(only_active)


def test_ruproxy_client_without_env(monkeypatch, tmp_path):
    monkeypatch.delenv("LANDCHECK_RU_PROXY", raising=False)
    monkeypatch.setenv("LANDCHECK_RU_CA_BUNDLE", str(tmp_path / "missing.pem"))
    assert ruproxy.status() == {"enabled": False, "proxy": None, "ca_bundle": None}
    with ruproxy.client(timeout=1) as c:
        assert c.headers["User-Agent"].startswith("Mozilla/5.0")
    monkeypatch.setenv("LANDCHECK_RU_PROXY", "socks5h://127.0.0.1:1080")
    assert ruproxy.enabled() and ruproxy.proxy_url() == "socks5h://127.0.0.1:1080"
    with pytest.raises(httpx.ConnectError):
        with ruproxy.client(timeout=1) as c:
            c.get("https://torgi.gov.ru/")
