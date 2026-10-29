"""R9: слежение за участком — снимок, разница, обход и уведомление."""

from landcheck.web.watch import diff, diff_text, snapshot


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
