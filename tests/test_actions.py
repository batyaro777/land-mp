"""R7: «План действий» по рискам."""

from landcheck.actions import action_plan


def _flag(rule_id, level, message="", details=None):
    return {
        "rule_id": rule_id,
        "level": level,
        "message": message,
        "source": "",
        "details": details or {},
    }


def test_action_plan_steps_by_flag_and_documents_deduped():
    item = {
        "flags": [
            _flag("R13", "ок", "участок встречается в 2 объявлениях"),
            _flag("R05", "проверить", "ВРИ не подтверждает цель"),
            _flag(
                "R10",
                "проверить",
                "участок пересекает ЗОУИТ (3)",
                {
                    "zouit": [
                        {"kind": "Санитарно-защитная зона", "name": "СЗЗ завода"},
                        {
                            "kind": "Охранная зона объектов электросетевого хозяйства",
                            "name": "ЛЭП 10 кВ",
                        },
                        {"kind": "Прочая зона", "name": "неизвестно"},
                    ]
                },
            ),
            _flag("R03", "стоп", "границы участка в ЕГРН не установлены"),
            _flag("R02", "стоп", "статус «архивный»: участок снят с учёта"),
            _flag("R99", "проверить", "неизвестное правило"),
        ]
    }
    plan = action_plan(item)
    assert [s["rule_id"] for s in plan["steps"]] == ["R03", "R02", "R05", "R10"]  # стоп раньше
    r10 = plan["steps"][3]
    assert len(r10["actions"]) == 3
    assert "Роспотребнадзор" in r10["actions"][0]["where"]
    assert "сетевая организация" in r10["actions"][1]["where"]
    assert r10["actions"][2]["what"].startswith("Вид и режим зоны")
    assert all({"what", "where", "next"} <= set(a) for s in plan["steps"] for a in s["actions"])
    names = [d["name"] for d in plan["documents"]]
    assert names.count("Выписка ЕГРН об объекте не старше 30 дней") == 1
    assert "Межевой план и акт согласования границ" in names
    assert any(d["name"].startswith("ГПЗУ") for d in plan["documents"])


def test_action_plan_levels_and_variants():
    stop = action_plan({"flags": [_flag("R04", "стоп", "цель «ИЖС»: сельхозземля")]})
    assert "172-ФЗ" in stop["steps"][0]["actions"][0]["next"]
    check = action_plan(
        {"flags": [_flag("R11", "проверить", "территориальная зона в НСПД не найдена")]}
    )
    assert "ПЗЗ" in check["steps"][0]["actions"][0]["what"]
    temp = action_plan(
        {"flags": [_flag("R02", "проверить", "статус «временный»: права не зарегистрированы")]}
    )
    assert "регистрации права" in temp["steps"][0]["actions"][0]["what"]
    assert action_plan({"flags": []}) == {"steps": [], "documents": []}
    assert action_plan({}) == {"steps": [], "documents": []}
