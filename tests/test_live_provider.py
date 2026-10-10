"""Провайдер НСПД на моках pynspd: проверяем сборку payload, сеть не нужна."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from landcheck.normalize import parcel_from_payload
from landcheck.providers.nspd import LiveNspdProvider
from landcheck.rules.purpose import category_class

pynspd = pytest.importorskip("pynspd")


class FakeGeometry:
    type = "Polygon"

    def to_shape(self):
        from shapely.geometry import box

        return box(37.0, 55.0, 37.001, 55.001)


class FakeFeat:
    def __init__(
        self, cad_num: str, *, no_coords: bool = False, category: str = "Земли населенных пунктов"
    ):
        self.id = 1
        self.geometry = FakeGeometry()
        self.properties = SimpleNamespace(
            category=36368,
            options=SimpleNamespace(cad_num=cad_num, no_coords=no_coords),
        )
        self._dump = {
            "type": "Feature",
            "id": 1,
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[37.0, 55.0], [37.001, 55.0], [37.001, 55.001], [37.0, 55.0]]],
            },
            "properties": {
                "category": 36368,
                "categoryName": "Земельные участки из ЕГРН",
                "options": {
                    "cad_num": cad_num,
                    "status": "Учтенный",
                    "land_record_category_type": category,
                    "permitted_use_established_by_document": "Для ИЖС",
                    "specified_area": 1000.0,
                    "cost_value": 1_500_000.0,
                    "ownership_type": "Частная собственность",
                    "geocoderObject": no_coords,
                },
            },
        }

    def model_dump(self, **_):
        return self._dump


class FakeZone:
    def __init__(self, layer: str, reg: str):
        self._dump = {
            "type": "Feature",
            "id": 2,
            "geometry": {"type": "Polygon", "coordinates": []},
            "properties": {
                "category": 36940,
                "categoryName": layer,
                "options": {"reg_numb_border": reg},
            },
        }

    def model_dump(self, **_):
        return self._dump


class FakeNspd:
    def __init__(self, feat, *, ambiguous=False, tab_error=False):
        self.feat, self.ambiguous, self.tab_error = feat, ambiguous, tab_error
        self.calls: list[str] = []

    def find_landplot(self, cn):
        self.calls.append("find")
        if self.ambiguous:
            raise pynspd.errors.AmbiguousSearchError(cn)
        return self.feat

    def search_landplots(self, cn):
        self.calls.append("search")
        return [self.feat] if self.feat else None

    def tab_permission_type(self, feat):
        self.calls.append("tab")
        if self.tab_error:
            raise RuntimeError("tab down")
        return ["Для индивидуального жилищного строительства (2.1)"]

    def search_in_contour(self, contour, layer_def):
        self.calls.append(f"contour:{layer_def.layer_meta.title}")
        if "ЗОУИТ" in layer_def.layer_meta.title:
            return [FakeZone("ЗОУИТ объектов энергетики, связи, транспорта", "50:09-6.1")]
        return [FakeZone("Территориальные зоны", "50:09-7.1")]

    def close(self):
        pass


def provider_with(fake: FakeNspd, **kw) -> LiveNspdProvider:
    prov = LiveNspdProvider(**kw)
    prov._client = lambda: fake  # type: ignore[method-assign]
    return prov


def test_full_payload_and_normalization():
    fake = FakeNspd(FakeFeat("50:09:0060202:101"))
    payload = provider_with(fake).fetch_raw("50:09:0060202:101")
    assert payload["found"] and payload["provider"] == "nspd"
    assert payload["permission_types"] == ["Для индивидуального жилищного строительства (2.1)"]
    assert len(payload["zouit"]) == 1 and len(payload["territorial_zones"]) == 1
    assert fake.calls == ["find", "tab", "contour:Иные ЗОУИТ", "contour:Территориальные зоны"]
    parcel = parcel_from_payload(payload)
    assert parcel.has_borders and parcel.zouit[0].reg_number == "50:09-6.1"
    assert category_class(parcel.category) == "settlement"
    assert parcel.cadastral_cost_rub == 1_500_000


def test_not_found_and_no_zones_for_parcel_without_coords():
    assert provider_with(FakeNspd(None)).fetch_raw("50:09:0060202:1")["found"] is False
    fake = FakeNspd(FakeFeat("50:09:0060202:2", no_coords=True))
    payload = provider_with(fake).fetch_raw("50:09:0060202:2")
    assert payload["found"] and payload["zouit"] is None and payload["territorial_zones"] is None
    assert not parcel_from_payload(payload).has_borders


def test_ambiguous_search_falls_back_to_exact_match_and_tab_error_is_soft():
    fake = FakeNspd(FakeFeat("50:09:0060202:3"), ambiguous=True, tab_error=True)
    payload = provider_with(fake, with_zones=False).fetch_raw("50:09:0060202:3")
    assert payload["found"] and "search" in fake.calls
    assert payload["permission_types"] is None
    assert any("ВРИ" in e for e in payload["errors"])
