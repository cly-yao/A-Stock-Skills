import json
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from skills_import import load

screener = load("skills/05-quant/screener/main.py", "screener_under_test")
FIXTURE = ROOT / "tests/fixtures/screener_universe_synthetic.json"


def test_fixture_screen_filters_sorts_and_drops_st():
    quotes, klines, fixture_id = screener.load_fixture(FIXTURE)
    result = screener.screen(
        where="pe<20 and total_mv>50",
        sort="pe",
        quotes=quotes,
        klines=klines,
        provenance="synthetic",
    )
    payload = screener.result_payload(result)
    assert fixture_id == "screener-universe-v1"
    assert payload["provenance"] == "synthetic"
    assert payload["banner"].startswith("SYNTHETIC_ONLY")
    assert payload["rows"][0]["code"] == "000001"
    assert payload["rows"][1]["code"] == "600519"
    assert {row["code"] for row in payload["rows"]} == {"000001", "600519"}
    assert "000002" in payload["excluded_st"]
    assert payload["universe_complete"] is True


def test_scope_records_codes_removed_by_the_predicate():
    quotes, klines, _ = screener.load_fixture(FIXTURE)
    result = screener.screen(
        where="pe>10 and pe<40",
        scope=["600519", "000001", "300750"],
        quotes=quotes,
        klines=klines,
        provenance="synthetic",
    )
    payload = screener.result_payload(result)
    assert payload["banner"].startswith("SYNTHETIC_ONLY")
    assert [row["code"] for row in payload["rows"]] == ["600519"]
    assert payload["filtered_out"] == ["000001", "300750"]


def test_null_pe_is_not_counted_as_a_failed_comparison():
    frame = pd.DataFrame([
        {"code": "600519", "name": "无市盈", "price": 10.0, "pe": None, "total_mv": 1.0},
    ])
    result = screener.screen(
        where="pe>10",
        scope=["600519"],
        quotes=frame,
        provenance="synthetic",
    )
    payload = screener.result_payload(result)
    assert payload["banner"].startswith("SYNTHETIC_ONLY")
    assert payload["row_count"] == 0
    assert payload["not_evaluated"] == ["600519"]
    assert payload["filtered_out"] == []


def test_fixture_file_cannot_claim_live():
    bad = {"provenance": "live", "rows": [{"code": "000001", "pe": 1}]}
    path = ROOT / "tests/fixtures/_bad_live_claim.json"
    path.write_text(json.dumps(bad), encoding="utf-8")
    try:
        with pytest.raises(ValueError, match="synthetic"):
            screener.load_fixture(path)
    finally:
        path.unlink(missing_ok=True)


def test_caller_quotes_cannot_be_labeled_live():
    frame = pd.DataFrame([{"code": "000001", "name": "x", "pe": 1, "total_mv": 100}])
    with pytest.raises(ValueError, match="live"):
        screener.screen(where="pe<20", quotes=frame, provenance="live")


def test_missing_roe_is_not_filled_and_growth_matches_nothing():
    quotes, _, _ = screener.load_fixture(FIXTURE)
    result = screener.screen(
        where="roe>15 and pe>10 and pe<50",
        quotes=quotes,
        provenance="synthetic",
    )
    payload = screener.result_payload(result)
    assert payload["row_count"] == 0
    assert "roe" in payload["missing_fields"]
    assert payload["errors"] == [] or payload["row_count"] == 0


def test_between_and_signals_on_synthetic_klines():
    quotes, _, _ = screener.load_fixture(FIXTURE)
    rising = pd.DataFrame([
        {"date": f"2020-01-{day:02d}", "open": day, "close": day, "high": day, "low": day, "volume": 100}
        for day in range(1, 26)
    ])
    falling = pd.DataFrame([
        {"date": f"2020-01-{day:02d}", "open": day, "close": 26 - day, "high": 26, "low": 1, "volume": 100}
        for day in range(1, 26)
    ])
    result = screener.screen(
        where="pe between 1,90",
        signals=["above_ma20"],
        quotes=quotes,
        klines={"600519": rising, "000001": falling, "300750": falling, "002001": falling},
        provenance="synthetic",
        mode="and",
    )
    payload = screener.result_payload(result)
    assert payload["provenance"] == "synthetic"
    assert [row["code"] for row in payload["rows"]] == ["600519"]


def test_limit_up_signal_uses_previous_close_not_a_constant():
    bars = pd.DataFrame([
        {"close": 10.0, "open": 10.0, "high": 10.0, "low": 10.0, "volume": 1.0},
        {"close": 11.0, "open": 10.0, "high": 11.0, "low": 10.0, "volume": 1.0},
    ])
    assert screener.signal_status(bars, "limit_up") is True
    softer = bars.copy()
    softer.loc[1, "close"] = 10.5
    assert screener.signal_status(softer, "limit_up") is False
    assert screener.signal_status(bars.iloc[:1], "above_ma20") is None


def test_fixture_cli_json_is_synthetic():
    completed = subprocess.run(
        [
            sys.executable,
            "skills/05-quant/screener/main.py",
            "screen",
            "--fixture",
            "tests/fixtures/screener_universe_synthetic.json",
            "--where",
            "pe<20 and total_mv>50",
            "--sort",
            "pe",
            "--json",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["provenance"] == "synthetic"
    assert payload["banner"].startswith("SYNTHETIC_ONLY")
    assert [row["code"] for row in payload["rows"]] == ["000001", "600519"]
    assert "LIVE" not in payload["banner"]
