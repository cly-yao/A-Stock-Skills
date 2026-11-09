import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import astock_runtime
from skills_import import load

report = load("skills/05-reports/report/main.py", "report_under_test")


class FakeResponse:
    def __init__(self, text="", payload=None, status=200):
        self.text = text
        self._payload = payload
        self.status_code = status
        self.encoding = None

    def json(self):
        return self._payload


class FakeTransport:
    provenance = "synthetic"

    def __init__(self, handler, max_calls=6, timeout=8):
        self.handler = handler
        self.max_calls = max_calls
        self.timeout = timeout
        self.calls = []

    def request(self, method, url, params=None, encoding=None, headers=None):
        if len(self.calls) >= self.max_calls:
            raise astock_runtime.BudgetExceeded("test budget")
        self.calls.append({
            "method": method,
            "url": url,
            "params": params,
            "ok": True,
            "error": None,
        })
        return self.handler(url, params)


def _bars(n=20):
    rows = []
    for day in range(1, n + 1):
        rows.append(["2020-01-%02d" % day, day, day, day, day, 1000])
    return rows


def _route(url, params):
    if "hq.sinajs.cn" in url:
        return FakeResponse(text=(
            'var hq_str_s_sh000001="合成上证,1.50,0.10,0.20,1,2";\n'
            'var hq_str_s_sz399001="合成深成,2.50,0.20,0.30,1,2";\n'
        ))
    if "getTopicZDFenBu" in url:
        return FakeResponse(payload={"data": {"qdate": 20200101, "fenbu": [{"1": 3}, {"-2": 4}, {"0": 1}]}})
    if "clist/get" in url:
        assert params["pz"] == "5"
        assert params["pn"] == "1"
        return FakeResponse(payload={"data": {"total": 9, "diff": [
            {"f12": "BK1", "f14": "合成行业", "f3": 1.5, "f128": "合成龙头", "f140": "000001"}
        ]}})
    if "getTopicZTPool" in url:
        assert params["pagesize"] == "5"
        return FakeResponse(payload={"data": {"tc": 10, "qdate": 20200101, "pool": [
            {"c": "000001", "n": "合成", "zdp": 10.0, "hybk": "测试", "p": 1000}
        ]}})
    if "ulist.np" in url:
        return FakeResponse(payload={"data": {"diff": [{
            "f12": "600519", "f14": "合成茅台", "f2": 10, "f3": 1.5,
            "f6": 100, "f8": 0.2, "f9": 15, "f20": 2e10, "f21": 2e10, "f23": 2,
        }]}})
    if "fqkline" in url:
        return FakeResponse(payload={"code": 0, "data": {"sh600519": {"qfqday": _bars(20)}}})
    if "fflow" in url:
        assert params["lmt"] == "5"
        return FakeResponse(payload={"data": {"klines": ["2020-01-02,1000,1"]}})
    if "search/jsonp" in url:
        return FakeResponse(text='cb({"hitsTotal":1,"result":{"cmsArticleWebOld":[{"title":"合成标题","date":"2020-01-01","mediaName":"测试","url":"http://example.test"}]}})')
    raise AssertionError(url)


def test_daily_synthetic_transport_is_bounded_and_labeled():
    transport = FakeTransport(_route, max_calls=6)
    built = report.build_daily_report(transport=transport, provenance="synthetic")
    text = report.render_markdown(built)
    assert built["provenance"] == "synthetic"
    assert "SYNTHETIC_ONLY" in text
    assert "LIVE —" not in text
    assert len(transport.calls) == 4
    assert all(item["params"] is None or item["params"].get("pz") in (None, "5") for item in transport.calls)
    breadth = next(section for section in built["sections"] if section["id"] == "breadth")
    assert breadth["up"] == 3 and breadth["down"] == 4 and breadth["flat"] == 1
    limit_up = next(section for section in built["sections"] if section["id"] == "limit_up")
    assert limit_up["status"] == "partial"
    assert limit_up["total"] == 10
    assert "p_raw" in text


def test_injected_transport_cannot_be_called_live():
    with pytest.raises(ValueError, match="live"):
        report.build_daily_report(transport=FakeTransport(_route), provenance="live")


def test_budget_skips_later_sections_without_inventing_them():
    transport = FakeTransport(_route, max_calls=1)
    built = report.build_daily_report(transport=transport, provenance="synthetic")
    statuses = [section["status"] for section in built["sections"]]
    assert statuses[0] in {"partial", "ok"}
    assert statuses[1:] == ["skipped", "skipped", "skipped"]
    text = report.render_markdown(built)
    assert "预算用尽" in text
    assert "上涨桶" not in text.split("市场广度", 1)[1].split("##", 1)[0]


def test_stock_report_uses_one_quote_and_computes_ma_from_fixture_bars():
    transport = FakeTransport(_route, max_calls=5)
    built = report.build_stock_report("600519", transport=transport, provenance="synthetic")
    text = report.render_markdown(built)
    assert len(transport.calls) == 4
    assert "SYNTHETIC_ONLY" in text
    quote = next(section for section in built["sections"] if section["id"] == "quote")
    assert quote["rows"][0]["price"] == 10
    assert quote["rows"][0]["total_mv"] == pytest.approx(200)
    technical = next(section for section in built["sections"] if section["id"] == "technical")
    assert technical["rows"][0]["close"] == 20
    assert technical["rows"][0]["ma5"] == pytest.approx(18)
    assert technical["rows"][0]["ma20"] == pytest.approx(10.5)
    assert "stock_zh_a_spot" not in text


def test_failed_sections_do_not_render_zero_prices():
    def empty(url, params):
        if "jsonp" in url or "hq.sinajs" in url:
            return FakeResponse(text="")
        return FakeResponse(payload={"data": {}})

    built = report.build_stock_report("600519", transport=FakeTransport(empty), provenance="synthetic")
    text = report.render_markdown(built)
    assert all(section["status"] == "failed" for section in built["sections"])
    assert "现价:" not in text
    assert "0.00" not in text
    assert report._exit_code(built) == 1


def test_portfolio_omits_missing_quotes_from_the_total():
    def quotes(url, params):
        return FakeResponse(payload={"data": {"diff": [{
            "f12": "600519", "f14": "合成茅台", "f2": 10, "f3": 1,
            "f9": 15, "f20": 1e8, "f21": 1e8, "f23": 1,
        }]}})

    positions = [
        {"code": "600519", "name": "合成茅台", "cost": 8, "shares": 10},
        {"code": "000001", "name": "缺失", "cost": 5, "shares": 10},
    ]
    built = report.build_portfolio_report(
        positions=positions,
        transport=FakeTransport(quotes),
        provenance="synthetic",
    )
    section = built["sections"][0]
    assert section["status"] == "partial"
    assert section["missing"] == ["000001"]
    text = report.render_markdown(built)
    assert "计入合计的持仓: 1 只" in text
    assert "100.00" in text  # 10 * 10
    assert "SYNTHETIC_ONLY" in text
