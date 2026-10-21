import json
from pathlib import Path

import pytest

from tests.helpers import make_payload, zone


@pytest.fixture(autouse=True)
def _no_elevation_network(monkeypatch):
    """Рельеф в тестах не ходит в сеть: модуль выключен, кроме явных проверок с fetch."""
    monkeypatch.setenv("LANDCHECK_ELEVATION_URL", "off")
    monkeypatch.setenv("LANDCHECK_OVERPASS_URL", "off")


TZ = [zone("Территориальные зоны", "50:09-7.1", "Ж-1", "Зона застройки ИЖС")]

# Синтетический набор: 8 участков с разными дефектами + 1 отсутствующий.
SAMPLE = {
    "50:09:0060202:101": make_payload("50:09:0060202:101", tz=TZ),  # эталон: ИЖС, всё ок
    "50:09:0060202:102": make_payload(
        "50:09:0060202:102",
        category="Земли сельскохозяйственного назначения",
        vri="Для сельскохозяйственного производства",
        cost=300_000,
        tz=TZ,
    ),
    "50:09:0060202:103": make_payload(
        "50:09:0060202:103",
        borders=False,
        specified_area=None,
        declared_area=1000.0,
        area=1000.0,
        tz=TZ,
    ),
    "50:09:0060202:104": make_payload(
        "50:09:0060202:104",
        zouit=[
            zone("ЗОУИТ объектов энергетики, связи, транспорта", "50:09-6.123", "Охранная зона ЛЭП")
        ],
        tz=TZ,
    ),
    "50:09:0060202:105": make_payload("50:09:0060202:105", status="Архивный", tz=TZ),
    "50:09:0060202:106": make_payload(
        "50:09:0060202:106", ownership="Собственность публично-правовых образований", tz=[]
    ),
    "50:09:0060202:107": make_payload(
        "50:09:0060202:107",
        vri="Магазины",
        tz=[zone("Территориальные зоны", "50:09-7.1", "Ж-1", "Зона застройки ИЖС")],
    ),
    "50:09:0060202:108": make_payload(
        "50:09:0060202:108", area=1200.0, specified_area=1200.0, cost=1_800_000, tz=TZ
    ),
    "50:09:0060202:999": make_payload("50:09:0060202:999", found=False),
}


@pytest.fixture
def fixtures_dir(tmp_path: Path) -> Path:
    d = tmp_path / "fixtures"
    d.mkdir()
    for cn, payload in SAMPLE.items():
        (d / (cn.replace(":", "_") + ".json")).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
    return d


@pytest.fixture
def sample_csv(tmp_path: Path) -> Path:
    p = tmp_path / "podborka.csv"
    p.write_text(
        "Кадастровый номер;Цена, руб;Площадь, сот;Ссылка\n"
        "50:09:0060202:101;2500000;10;https://example.com/1\n"
        "50:09:0060202:102;900000;10;https://example.com/2\n"
        "50:09:0060202:103;2400000;10;\n"
        "50:09:0060202:104;2600000;10;\n"
        "50:09:0060202:105;2000000;10;\n"
        "50:09:0060202:106;1500000;10;\n"
        "50:09:0060202:107;2700000;10;\n"
        "50:09:0060202:108;3100000;14;\n"
        "50:09:0060202:101;2450000;10;https://example.com/1-dup\n"
        "50:09:0060202:999;1000000;10;\n"
        "не номер;100;1;\n",
        encoding="utf-8",
    )
    return p
