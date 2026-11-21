"""constraints.txt фиксирует версии прода: каждая прямая зависимость из pyproject должна быть
закреплена, иначе сборка без правок кода подтянет новый пакет (Клавдий, 09.11)."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[1]


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _pinned() -> dict[str, str]:
    out: dict[str, str] = {}
    for line in (ROOT / "constraints.txt").read_text("utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        req = Requirement(line)
        spec = str(req.specifier)
        assert spec.startswith("=="), f"не точная версия: {line}"
        out[_norm(req.name)] = spec[2:]
    return out


def test_every_direct_dependency_is_pinned():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))
    direct = [Requirement(d) for d in data["project"]["dependencies"]]
    for extra in ("asr-local",):
        direct += [Requirement(d) for d in data["project"]["optional-dependencies"][extra]]
    pinned = _pinned()
    missing = [r.name for r in direct if _norm(r.name) not in pinned]
    assert not missing, f"нет в constraints.txt: {missing}"
    # закреплённая версия должна удовлетворять ограничению из pyproject
    for r in direct:
        assert r.specifier.contains(pinned[_norm(r.name)], prereleases=True), (
            r.name,
            pinned[_norm(r.name)],
            str(r.specifier),
        )


def test_dockerfile_and_ci_use_constraints():
    assert "-c constraints.txt" in (ROOT / "Dockerfile").read_text("utf-8")
    assert "constraints.txt ./" in (ROOT / "Dockerfile").read_text("utf-8")
    assert "-c constraints.txt" in (ROOT / ".github" / "workflows" / "ci.yml").read_text("utf-8")
