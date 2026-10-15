"""Офлайн-провайдер: payload из JSON-файлов. Для тестов и ручного прототипа без сети."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from landcheck.providers.base import empty_payload


def fixture_filename(cad_num: str) -> str:
    return cad_num.replace(":", "_") + ".json"


class FixtureProvider:
    name = "fixture"

    def __init__(self, directory: str | Path, *, strict: bool = False):
        self.directory = Path(directory)
        self.strict = strict

    def fetch_raw(self, cad_num: str) -> dict[str, Any]:
        path = self.directory / fixture_filename(cad_num)
        if not path.exists():
            if self.strict:
                raise FileNotFoundError(path)
            payload = empty_payload(cad_num, self.name)
            payload["errors"].append(f"фикстура не найдена: {path.name}")
            return payload
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload.setdefault("cad_num", cad_num)
        payload.setdefault("provider", self.name)
        payload.setdefault("errors", [])
        payload.setdefault("found", payload.get("landplot") is not None)
        return payload

    def fetch_location_raw(self, cad_num: str, parcel_geojson: dict[str, Any]) -> dict[str, Any]:
        """Окружение из `<КН>.location.json`, если файл есть; иначе пустой ответ без ошибок."""
        from landcheck.location import empty_location_raw

        path = self.directory / (cad_num.replace(":", "_") + ".location.json")
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            raw.setdefault("cad_num", cad_num)
            return raw
        raw = empty_location_raw(cad_num)
        raw["nspd"]["errors"].append("фикстура окружения не найдена")
        return raw

    def save(self, payload: dict[str, Any]) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / fixture_filename(payload["cad_num"])
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), "utf-8")
        return path
