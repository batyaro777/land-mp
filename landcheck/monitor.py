"""Мониторинг формата НСПД: ежедневный прогон эталонных номеров и сравнение со снимком.

Снимок хранит набор ключей options карточки и стабильные поля по каждому номеру.
Дрейф (пропали ключи, участок перестал находиться, изменилась категория/площадь) → код 2.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from landcheck.normalize import parcel_from_payload
from landcheck.providers.base import ParcelProvider

STABLE_FIELDS = ("category", "status", "best_area_m2", "has_borders")
REQUIRED_OPTION_KEYS = {
    "cad_num",
    "status",
    "land_record_category_type",
    "permitted_use_established_by_document",
    "cost_value",
}


@dataclass
class MonitorReport:
    checked_at: datetime
    total: int
    found: int
    missing_keys: dict[str, list[str]] = field(default_factory=dict)
    changed: dict[str, dict[str, Any]] = field(default_factory=dict)
    not_found: list[str] = field(default_factory=list)
    errors: dict[str, list[str]] = field(default_factory=dict)

    @property
    def drift(self) -> bool:
        return bool(self.missing_keys or self.changed or self.not_found or self.errors)

    def summary(self) -> str:
        lines = [
            f"НСПД мониторинг {self.checked_at:%d.%m.%Y %H:%M} UTC: "
            f"найдено {self.found}/{self.total}",
        ]
        for cn in self.not_found:
            lines.append(f"  НЕ НАЙДЕН: {cn}")
        for cn, keys in self.missing_keys.items():
            lines.append(f"  {cn}: пропали ключи {', '.join(sorted(keys))}")
        for cn, diff in self.changed.items():
            for k, (old, new) in diff.items():
                lines.append(f"  {cn}: {k} {old!r} → {new!r}")
        for cn, errs in self.errors.items():
            lines.append(f"  {cn}: ошибки: {'; '.join(errs)}")
        lines.append("ДРЕЙФ: формат или данные изменились" if self.drift else "ОК: без изменений")
        return "\n".join(lines)


def load_reference(path: str | Path) -> list[str]:
    from landcheck.inputs import CAD_RE, normalize_cad_num

    text = Path(path).read_text(encoding="utf-8")
    return [normalize_cad_num(m.group(0)) for m in CAD_RE.finditer(text)]


def _option_keys(payload: dict[str, Any]) -> set[str]:
    lp = payload.get("landplot") or {}
    opts = (lp.get("properties") or {}).get("options") or {}
    return set(opts.keys())


def _stable(payload: dict[str, Any]) -> dict[str, Any]:
    p = parcel_from_payload(payload)
    return {k: getattr(p, k) for k in STABLE_FIELDS}


def build_snapshot(provider: ParcelProvider, cad_nums: list[str]) -> dict[str, Any]:
    snap: dict[str, Any] = {"created_at": datetime.now(UTC).isoformat(), "parcels": {}}
    for cn in cad_nums:
        payload = provider.fetch_raw(cn)
        snap["parcels"][cn] = {
            "found": bool(payload.get("found")),
            "option_keys": sorted(_option_keys(payload)),
            "stable": _stable(payload) if payload.get("found") else {},
            "errors": payload.get("errors", []),
        }
    return snap


def run_monitor(
    provider: ParcelProvider, cad_nums: list[str], snapshot: dict[str, Any] | None
) -> MonitorReport:
    report = MonitorReport(checked_at=datetime.now(UTC), total=len(cad_nums), found=0)
    ref = (snapshot or {}).get("parcels", {})
    for cn in cad_nums:
        payload = provider.fetch_raw(cn)
        if payload.get("errors"):
            report.errors[cn] = list(payload["errors"])
        if not payload.get("found"):
            report.not_found.append(cn)
            continue
        report.found += 1
        keys = _option_keys(payload)
        expected = set(ref.get(cn, {}).get("option_keys") or []) | REQUIRED_OPTION_KEYS
        missing = expected - keys
        if missing:
            report.missing_keys[cn] = sorted(missing)
        if cn in ref and ref[cn].get("stable"):
            now = _stable(payload)
            diff = {
                k: (ref[cn]["stable"].get(k), now.get(k))
                for k in STABLE_FIELDS
                if ref[cn]["stable"].get(k) != now.get(k)
            }
            if diff:
                report.changed[cn] = diff
    return report


def save_snapshot(snapshot: dict[str, Any], path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return p


def load_snapshot(path: str | Path) -> dict[str, Any] | None:
    p = Path(path)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))
