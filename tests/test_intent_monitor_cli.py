import json
from pathlib import Path

from typer.testing import CliRunner

from landcheck.cli import app
from landcheck.intent import parse_request
from landcheck.models import Purpose
from landcheck.monitor import build_snapshot, run_monitor
from landcheck.providers.fixtures import FixtureProvider
from tests.helpers import make_payload

runner = CliRunner()


def test_parse_request():
    r = parse_request("Ищу под ИЖС до 3 млн, от 8 до 12 соток в Истре")
    assert r.purpose is Purpose.IZHS
    assert r.budget_rub == 3_000_000
    assert r.min_area_m2 == 800 and r.max_area_m2 == 1200
    r2 = parse_request("коммерция, бюджет 15 000 000 руб, 50 соток")
    assert r2.purpose is Purpose.COMMERCE and r2.budget_rub == 15_000_000
    assert r2.min_area_m2 == 4500 and r2.max_area_m2 == 7500
    assert parse_request("просто проверить").purpose is Purpose.ANY


def test_monitor_detects_drift(fixtures_dir: Path, tmp_path: Path):
    prov = FixtureProvider(fixtures_dir)
    cns = ["50:09:0060202:101", "50:09:0060202:102"]
    snap = build_snapshot(prov, cns)
    assert run_monitor(prov, cns, snap).drift is False
    # ломаем формат: пропал ключ, изменилась категория
    broken = make_payload("50:09:0060202:101", category="Земли запаса")
    del broken["landplot"]["properties"]["options"]["cost_value"]
    (fixtures_dir / "50_09_0060202_101.json").write_text(json.dumps(broken), encoding="utf-8")
    report = run_monitor(prov, cns, snap)
    assert report.drift
    assert "cost_value" in report.missing_keys["50:09:0060202:101"]
    assert "category" in report.changed["50:09:0060202:101"]
    assert "ДРЕЙФ" in report.summary()


def test_cli_check_with_fixtures(fixtures_dir: Path, sample_csv: Path, tmp_path: Path):
    out = tmp_path / "result.xlsx"
    result = runner.invoke(
        app,
        [
            "check",
            str(sample_csv),
            "--purpose",
            "izhs",
            "--fixtures",
            str(fixtures_dir),
            "--out",
            str(out),
            "--md",
        ],
    )
    assert result.exit_code == 0, result.output
    assert out.exists()
    assert "стоп" in result.output and "50:09:0060202:101" in result.output


def test_cli_rules_and_monitor(fixtures_dir: Path, tmp_path: Path):
    assert runner.invoke(app, ["rules"]).exit_code == 0
    ref = tmp_path / "ref.txt"
    ref.write_text("50:09:0060202:101\n50:09:0060202:999\n", encoding="utf-8")
    snap = tmp_path / "snap.json"
    r = runner.invoke(
        app,
        [
            "monitor",
            "--reference",
            str(ref),
            "--snapshot",
            str(snap),
            "--fixtures",
            str(fixtures_dir),
            "--update",
        ],
    )
    assert r.exit_code == 0, r.output
    r2 = runner.invoke(
        app,
        [
            "monitor",
            "--reference",
            str(ref),
            "--snapshot",
            str(snap),
            "--fixtures",
            str(fixtures_dir),
        ],
    )
    assert r2.exit_code == 2  # 999 не найден → дрейф
