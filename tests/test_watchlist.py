import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from skills_import import load

watch = load("skills/02-data-collection/watchlist-monitor/main.py", "watchlist_under_test")


def test_missing_quote_is_not_a_zero_price(monkeypatch):
    frame = pd.DataFrame([{
        "code": "600519",
        "name": "合成",
        "price": 10.0,
        "pct_change": 1.0,
        "amount": 100.0,
        "turnover": 0.1,
        "pe": 20.0,
        "pb": 5.0,
        "total_mv": 100.0,
        "circ_mv": 100.0,
        "source": "unit",
    }])

    def fake_fetch(codes):
        assert codes == ["600519", "000001"]
        return frame

    monkeypatch.setattr(watch, "_fetch_quote_frame", fake_fetch)
    quotes = watch.get_realtime_quotes(["600519", "000001"])
    found = {item["code"]: item for item in quotes}
    assert found["600519"]["found"] is True
    assert found["600519"]["price"] == 10
    assert found["600519"]["total_mv"] == 100 * 1e8
    assert found["000001"]["found"] is False
    assert found["000001"]["price"] is None
    alerts = watch.check_alerts(
        quotes,
        {"price_below": 1, "pct_change_down": -1},
        [],
    )
    assert alerts == []


def test_too_many_codes_are_refused(monkeypatch):
    def boom(codes):
        raise AssertionError("should not fetch")

    monkeypatch.setattr(watch, "_fetch_quote_frame", boom)
    quotes = watch.get_realtime_quotes([f"{i:06d}" for i in range(51)])
    assert len(quotes) == 51
    assert all(item["price"] is None and item["found"] is False for item in quotes)


def test_volume_ratio_failure_is_none(monkeypatch):
    def boom(*args, **kwargs):
        raise TimeoutError("offline")

    monkeypatch.setattr("requests.get", boom)
    assert watch.get_volume_ratio("600519") is None
