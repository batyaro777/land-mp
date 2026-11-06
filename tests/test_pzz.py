"""Реестр ссылок ПЗЗ и ориентиры стоимости подключения (ТЗ v3, п. 4)."""

from __future__ import annotations

from landcheck import pzz


def test_municipal_links_by_address_and_region():
    r = pzz.links_for(
        {"cad_num": "40:10:160500:1", "address": "Калужская обл., р-н Козельский, д. Вырск"}
    )
    assert r["region"] == "Калужская область" and r["municipal"]["name"].startswith("Козельский")
    assert any("garant" in link["url"] for link in r["municipal"]["links"])
    assert r["federal"][0]["url"].startswith("https://fgistp") and "yandex" in r["search_url"]
    s = pzz.links_for({"cad_num": "23:49:0420002:1332", "address": "Краснодарский край, г. Сочи"})
    assert (
        s["municipal"]["links"][0]["url"].startswith("https://sochi.ru/")
        and s["municipal"]["links"][0]["verified"]
    )
    v = pzz.links_for(
        {"cad_num": "50:07:0070507:567", "address": "Московская область, Волоколамский г.о."}
    )
    assert v["municipal"]["name"].startswith("Волоколамский") and v["regional"]
    none = pzz.links_for(
        {"cad_num": "63:01:0810003:510", "address": "Самарская область, г. Самара"}
    )
    assert none["municipal"] is None and none["regional"] == [] and none["federal"]
    assert pzz.links_for({})["search_url"] is None


def test_electricity_and_gas_orientation():
    e = pzz.electricity_for({"cad_num": "50:07:0070507:567"})
    assert e["region"]["rate_rub_kw"] == 23859.53 and e["region"]["cost_15kw_rub"] == 357893
    assert "300-Р" in e["region"]["act"] and e["corridor_rub_kw"][1] == 23859.53
    k = pzz.electricity_for({"cad_num": "40:10:160500:1"})
    assert k["region"]["rate_rub_kw"] is None and "не найден" in k["region"]["act"]
    assert pzz.electricity_for({"cad_num": "63:01:1:1"})["region"] is None
    g = pzz.gas_info()
    assert g["links"][0]["url"].startswith("https://connectgas.ru/") and "1547" in g["acts"]
