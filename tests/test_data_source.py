import json
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import astock_runtime
from skills_import import load

RECORDED = json.loads(
    (ROOT / "tests/fixtures/sina_market_row.recorded.json").read_text(encoding="utf-8")
)


def test_recorded_sina_row_converts_mktcap_wan_to_yi():
    assert RECORDED["provenance"] == "recorded_live"
    rows = astock_runtime.normalize_sina_market_rows([RECORDED["row"]])
    assert rows[0]["code"] == "920000"
    assert rows[0]["pe"] == 15.625
    assert rows[0]["total_mv"] == 103140 / 10000
    assert rows[0]["total_mv_unit"] == "亿元"
    assert rows[0]["source"] == "sina_market_center"


def test_get_realtime_all_makes_one_call(monkeypatch):
    module = load("skills/01-infra/astock-data-source/main.py", "data_source_under_test")
    calls = []

    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return [{
                "code": "000001",
                "name": "单元测试",
                "trade": "10",
                "changepercent": 1,
                "pricechange": 0.1,
                "volume": 1,
                "amount": 2,
                "turnoverratio": 0.2,
                "per": 8,
                "pb": 0.7,
                "mktcap": 20000,
                "nmc": 10000,
            }]

    def fake_get(url, **kwargs):
        calls.append({"url": url, "timeout": kwargs.get("timeout"), "params": kwargs.get("params")})
        return Response()

    monkeypatch.setattr(requests, "get", fake_get)
    frame = module.get_realtime_all(num=80, timeout=3)
    assert len(calls) == 1
    assert calls[0]["params"]["num"] == 80
    assert calls[0]["timeout"] == 3
    assert frame.attrs["provenance"] == "live"
    assert frame.attrs["universe_complete"] is False
    assert frame.iloc[0]["total_mv"] == 2
    assert frame.iloc[0]["pe"] == 8
