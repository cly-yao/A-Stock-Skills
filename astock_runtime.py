#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared runtime helpers for this fork.

Public read-only HTTP only. No brokers, orders, or paid tokens.
Callers must label provenance: live, synthetic, recorded_live, or injected.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import ssl
import time
from pathlib import Path
from typing import Any, Iterable, Optional

import requests

PROVENANCE_LIVE = "live"
PROVENANCE_SYNTHETIC = "synthetic"
PROVENANCE_RECORDED = "recorded_live"
PROVENANCE_INJECTED = "injected"
ALLOWED_PROVENANCE = {
    PROVENANCE_LIVE,
    PROVENANCE_SYNTHETIC,
    PROVENANCE_RECORDED,
    PROVENANCE_INJECTED,
}

SINA_MC_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "Market_Center.getHQNodeData"
)
SINA_HQ_URL = "https://hq.sinajs.cn/list="
EM_ULIST_URL = "https://push2.eastmoney.com/api/qt/ulist.np/get"
EM_ULIST_URL_ALT = "https://82.push2.eastmoney.com/api/qt/ulist.np/get"
EM_CLIST_URL = "https://push2.eastmoney.com/api/qt/clist/get"
EM_CLIST_URL_ALT = "https://82.push2.eastmoney.com/api/qt/clist/get"
EM_FENBU_URL = "https://push2ex.eastmoney.com/getTopicZDFenBu"
EM_ZT_URL = "https://push2ex.eastmoney.com/getTopicZTPool"
EM_FFLOW_URL = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
EM_KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
EM_NEWS_URL = "https://search-api-web.eastmoney.com/search/jsonp"
IFZQ_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
SINA_KLINE_URL = (
    "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "CN_MarketData.getKLineData"
)
TENCENT_QT_URL = "https://qt.gtimg.cn/q="
TENCENT_FLOW_URL = "https://proxy.finance.qq.com/cgi/cgi-bin/fundflow/hsfundtab"
EM_F10_BULLETIN_URL = "https://emweb.securities.eastmoney.com/PC_HSF10/NewsBulletin/PageAjax"

# Sina mktcap / nmc are 万元. Screener strategies treat total_mv as 亿元.
SINA_MV_WAN_TO_YI = 10000.0

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; A-Stock-Skills-fork/runtime-fix)",
    "Referer": "https://quote.eastmoney.com/",
}

MAJOR_INDEX_SINA = [
    "s_sh000001",
    "s_sz399001",
    "s_sz399006",
    "s_sh000688",
    "s_sh000300",
    "s_sh000905",
    "s_sh000852",
    "s_sh000016",
]


class BudgetExceeded(RuntimeError):
    """Raised when another HTTP call would pass the call budget."""


def tls_verify():
    """CA bundle for requests. This never disables certificate checks.

    ``Session.trust_env`` is off so proxy variables are ignored, which also
    skips requests' own CA-bundle environment lookup. Resolve the bundle here:
    an existing ``SSL_CERT_FILE`` / ``REQUESTS_CA_BUNDLE`` / ``CURL_CA_BUNDLE``,
    then certifi, then the OpenSSL default file, then ``True``.
    """
    for key in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
        candidate = os.environ.get(key)
        if candidate and os.path.isfile(candidate):
            return candidate
    try:
        import certifi
        bundle = certifi.where()
        if bundle and os.path.isfile(bundle):
            return bundle
    except Exception:
        pass
    cafile = ssl.get_default_verify_paths().cafile
    if cafile and os.path.isfile(cafile):
        return cafile
    return True


def _require_verify(verify):
    if verify is False or verify is None:
        raise RuntimeError("TLS 证书校验不能关闭")
    if isinstance(verify, str) and not os.path.isfile(verify):
        raise RuntimeError(f"CA 证书包不存在: {verify}")
    return verify


class HttpBudget:
    """One short timeout per call, and a hard cap on how many calls run."""

    def __init__(self, max_calls: int = 6, timeout: float = 8.0, verify=None):
        if max_calls < 1:
            raise ValueError("max_calls 必须 >= 1")
        if timeout <= 0:
            raise ValueError("timeout 必须 > 0")
        self.max_calls = int(max_calls)
        self.timeout = float(timeout)
        self.verify = _require_verify(tls_verify() if verify is None else verify)
        self.calls: list[dict] = []
        self._session = requests.Session()
        self._session.trust_env = False
        self._session.verify = self.verify

    def request(
        self,
        method: str,
        url: str,
        params: Optional[dict] = None,
        headers: Optional[dict] = None,
        encoding: Optional[str] = None,
    ):
        if len(self.calls) >= self.max_calls:
            raise BudgetExceeded(
                f"HTTP 预算已用完 ({self.max_calls} 次)。未继续请求，未填入替代数据。"
            )
        rec = {
            "method": method.upper(),
            "url": url,
            "ok": False,
            "status": None,
            "error": None,
            "elapsed_sec": None,
        }
        started = time.time()
        try:
            response = self._session.request(
                method,
                url,
                params=params,
                headers=headers or DEFAULT_HEADERS,
                timeout=self.timeout,
                verify=self.verify,
                proxies={"http": None, "https": None},
            )
            rec["status"] = response.status_code
            if encoding:
                response.encoding = encoding
            response.raise_for_status()
            rec["ok"] = True
            return response
        except Exception as exc:
            rec["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            rec["elapsed_sec"] = round(time.time() - started, 3)
            self.calls.append(rec)


def http_get(url, *, params=None, headers=None, timeout=8, encoding=None):
    """GET with certificate verification left on. TLS errors propagate."""
    verify = _require_verify(tls_verify())
    response = requests.get(
        url,
        params=params,
        headers=headers,
        timeout=timeout,
        verify=verify,
        proxies={"http": None, "https": None},
    )
    if encoding:
        response.encoding = encoding
    return response


def repo_root_from(start: Path) -> Path:
    for candidate in [start, *start.parents]:
        if (candidate / "astock_runtime.py").is_file() and (candidate / "skills").is_dir():
            return candidate
    raise RuntimeError(f"找不到仓库根目录（自 {start} 向上）")


def load_py(path: Path, module_name: str):
    """Load a Python file whose path is not a valid import name."""
    path = Path(path)
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载 {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def normalize_code(code: str) -> str:
    text = str(code).strip().lower()
    text = re.sub(r"^(sh|sz|bj)\.?", "", text)
    digits = re.sub(r"\D", "", text)
    if not digits:
        raise ValueError(f"无法识别股票代码: {code}")
    return digits.zfill(6)


def em_secid(code: str) -> str:
    """Eastmoney secid. SH/STAR use market 1; other A-share codes use market 0.

    BJ codes are not specially mapped. Callers must not treat a missing BJ quote
    as a zero price.
    """
    code = normalize_code(code)
    market = "1" if code.startswith(("60", "68", "9")) else "0"
    return f"{market}.{code}"


def sina_symbol(code: str) -> str:
    code = normalize_code(code)
    prefix = "sh" if code.startswith(("60", "68", "9")) else "sz"
    return f"{prefix}{code}"


def to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if text in {"", "-", "--", "None", "nan", "NaN"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def normalize_sina_market_rows(rows: Iterable[dict]) -> list[dict]:
    """Map Sina Market_Center rows. total_mv is 亿元, converted from mktcap 万元."""
    normalized = []
    for row in rows:
        code = row.get("code")
        if code is None:
            continue
        mktcap_wan = to_float(row.get("mktcap"))
        nmc_wan = to_float(row.get("nmc"))
        normalized.append(
            {
                "code": normalize_code(code),
                "name": row.get("name") or "",
                "price": to_float(row.get("trade")),
                "pct_change": to_float(row.get("changepercent")),
                "change": to_float(row.get("pricechange")),
                "volume": to_float(row.get("volume")),
                "amount": to_float(row.get("amount")),
                "turnover": to_float(row.get("turnoverratio")),
                "pe": to_float(row.get("per")),
                "pb": to_float(row.get("pb")),
                "total_mv": None if mktcap_wan is None else mktcap_wan / SINA_MV_WAN_TO_YI,
                "circ_mv": None if nmc_wan is None else nmc_wan / SINA_MV_WAN_TO_YI,
                "total_mv_unit": "亿元",
                "total_mv_conversion": "sina.mktcap(万元)/10000",
                "source": "sina_market_center",
            }
        )
    return normalized


def normalize_ulist_rows(rows: Iterable[dict]) -> list[dict]:
    """Map Eastmoney ulist diff. f20/f21 are yuan; total_mv is 亿元."""
    normalized = []
    for row in rows:
        code = row.get("f12")
        if code is None:
            continue
        total_yuan = to_float(row.get("f20"))
        circ_yuan = to_float(row.get("f21"))
        normalized.append(
            {
                "code": normalize_code(code),
                "name": row.get("f14") or "",
                "price": to_float(row.get("f2")),
                "pct_change": to_float(row.get("f3")),
                "amount": to_float(row.get("f6")),
                "turnover": to_float(row.get("f8")),
                "pe": to_float(row.get("f9")),
                "pb": to_float(row.get("f23")),
                "total_mv": None if total_yuan is None else total_yuan / 1e8,
                "circ_mv": None if circ_yuan is None else circ_yuan / 1e8,
                "total_mv_unit": "亿元",
                "total_mv_conversion": "eastmoney.f20(元)/1e8",
                "source": "eastmoney_ulist",
            }
        )
    return normalized


def parse_sina_hq_batch(text: str) -> list[dict]:
    """Parse hq.sinajs.cn stock quotes. This feed has no PE/PB/market cap."""
    rows = []
    pattern = re.compile(r'var hq_str_([a-z]{2}\d+)="(.*)";')
    for match in pattern.finditer(text):
        symbol, body = match.group(1), match.group(2)
        if symbol.startswith("s_"):
            continue
        if not body:
            continue
        parts = body.split(",")
        if len(parts) < 10:
            continue
        price = to_float(parts[3])
        pre_close = to_float(parts[2])
        pct = None
        if price is not None and pre_close not in (None, 0):
            pct = (price - pre_close) / pre_close * 100
        rows.append(
            {
                "code": normalize_code(symbol[2:]),
                "name": parts[0],
                "price": price,
                "open": to_float(parts[1]),
                "pre_close": pre_close,
                "pct_change": pct,
                "high": to_float(parts[4]),
                "low": to_float(parts[5]),
                "volume": to_float(parts[8]),
                "amount": to_float(parts[9]),
                "pe": None,
                "pb": None,
                "total_mv": None,
                "circ_mv": None,
                "turnover": None,
                "total_mv_unit": "亿元",
                "source": "sina_hq",
                "missing_fields": ["pe", "pb", "total_mv", "circ_mv", "turnover"],
            }
        )
    return rows


def parse_sina_index_quotes(text: str) -> list[dict]:
    """Parse s_sh / s_sz simplified index quotes: name, price, change, pct, volume, amount."""
    rows = []
    pattern = re.compile(r'var hq_str_(s_[a-z]{2}\d+)="(.*)";')
    for match in pattern.finditer(text):
        symbol, body = match.group(1), match.group(2)
        if not body:
            continue
        parts = body.split(",")
        if len(parts) < 4:
            continue
        rows.append(
            {
                "symbol": symbol,
                "code": symbol.split("_", 1)[-1][2:],
                "name": parts[0],
                "price": to_float(parts[1]),
                "change": to_float(parts[2]),
                "pct_change": to_float(parts[3]),
                "volume": to_float(parts[4]) if len(parts) > 4 else None,
                "amount": to_float(parts[5]) if len(parts) > 5 else None,
                "source": "sina_index_s",
            }
        )
    return rows


def parse_tencent_quotes(text: str, include_fundamentals: bool = True) -> list[dict]:
    """Parse qt.gtimg.cn quotes.

    Field numbers were checked on 2026-11-09 for sh600519 against an Eastmoney
    ulist print of the same name: field 3 was the price, field 46 matched PB,
    field 44 was total market cap in 亿元, and field 52 matched 动态市盈率
    (20.97 at price 1442.17). Field 39 is a different PE and is not copied into
    ``pe``. Volume field 6 is 手, stored here as shares (×100). ``as_of`` is the
    exchange timestamp in the payload, labeled Asia/Shanghai, not converted.
    """
    rows = []
    pattern = re.compile(r'v_([a-z]{2}\d+)="([^"]*)"')
    for match in pattern.finditer(text):
        symbol, body = match.group(1), match.group(2)
        if not body or "~" not in body:
            continue
        parts = body.split("~")
        if len(parts) < 35:
            continue
        price = to_float(parts[3])
        if price is None:
            continue
        code_text = parts[2] if len(parts) > 2 and parts[2] else symbol[2:]
        try:
            code = normalize_code(code_text)
        except ValueError:
            continue
        amount = None
        if len(parts) > 35 and "/" in parts[35]:
            bits = parts[35].split("/")
            if len(bits) >= 3:
                amount = to_float(bits[2])
        volume_hand = to_float(parts[6]) if len(parts) > 6 else None
        as_of_raw = parts[30] if len(parts) > 30 else ""
        as_of = None
        if len(as_of_raw) == 14 and as_of_raw.isdigit():
            as_of = (
                f"{as_of_raw[0:4]}-{as_of_raw[4:6]}-{as_of_raw[6:8]} "
                f"{as_of_raw[8:10]}:{as_of_raw[10:12]}:{as_of_raw[12:14]} Asia/Shanghai"
            )
        row = {
            "code": code,
            "name": parts[1] if len(parts) > 1 else "",
            "price": price,
            "pre_close": to_float(parts[4]) if len(parts) > 4 else None,
            "open": to_float(parts[5]) if len(parts) > 5 else None,
            "high": to_float(parts[33]) if len(parts) > 33 else None,
            "low": to_float(parts[34]) if len(parts) > 34 else None,
            "change": to_float(parts[31]) if len(parts) > 31 else None,
            "pct_change": to_float(parts[32]) if len(parts) > 32 else None,
            "volume": None if volume_hand is None else volume_hand * 100,
            "volume_unit": "股",
            "volume_conversion": "tencent field 6 (手) * 100",
            "amount": amount,
            "turnover": to_float(parts[38]) if len(parts) > 38 else None,
            "pe": None,
            "pb": None,
            "total_mv": None,
            "circ_mv": None,
            "total_mv_unit": "亿元",
            "as_of": as_of,
            "source": "tencent_qt",
        }
        if include_fundamentals and len(parts) > 52:
            row["pe"] = to_float(parts[52])
            row["pb"] = to_float(parts[46]) if len(parts) > 46 else None
            row["total_mv"] = to_float(parts[44]) if len(parts) > 44 else None
            row["circ_mv"] = to_float(parts[45]) if len(parts) > 45 else None
            row["total_mv_conversion"] = "tencent field 44 (亿元); pe is field 52, not field 39"
        rows.append(row)
    return rows


def em_f10_code(code: str) -> str:
    """Eastmoney F10 prefix. SH for 60/68/9, otherwise SZ. BJ is not specially mapped."""
    code = normalize_code(code)
    prefix = "SH" if code.startswith(("60", "68", "9")) else "SZ"
    return prefix + code


def bulletin_detail_url(code: str, art_code: str) -> Optional[str]:
    """Public notice page. Checked for 600519 / AN202611021831320924 on 2026-11-09."""
    code = normalize_code(code)
    if not re.fullmatch(r"AN\d+", str(art_code or "")):
        return None
    return f"https://data.eastmoney.com/notices/detail/{code}/{art_code}.html"


def parse_f10_bulletins(payload, code: str) -> list[dict]:
    """Company filings from F10 ``gsgg`` only.

    ``gszx`` on the same response is not symbol-filtered (a SH600519 request
    included an unrelated industry brief) and is ignored.
    """
    if not isinstance(payload, dict):
        return []
    filings = payload.get("gsgg")
    if not isinstance(filings, list):
        return []
    code = normalize_code(code)
    rows = []
    for item in filings:
        if not isinstance(item, dict):
            continue
        art_code = str(item.get("art_code") or "")
        url = bulletin_detail_url(code, art_code)
        title = item.get("title")
        notice_date = item.get("notice_date")
        if not url or not title or not notice_date:
            continue
        date_text = str(notice_date)
        if len(date_text) >= 10 and re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_text[:10]):
            date_text = date_text[:10]
        rows.append({
            "title": title,
            "date": date_text,
            "url": url,
            "art_code": art_code,
            "requested_code": code,
            "source": "eastmoney_f10_bulletin",
        })
    return rows


def parse_eastmoney_kline(payload, code: str, fqt: str) -> list[dict]:
    """Eastmoney daily kline. fqt 0/1/2 is raw/qfq/hfq. Other values are rejected."""
    adjust = {"0": "raw", "1": "qfq", "2": "hfq"}.get(str(fqt))
    if adjust is None or not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, dict):
        return []
    klines = data.get("klines")
    if not isinstance(klines, list):
        return []
    code = normalize_code(code)
    rows = []
    for line in klines:
        parts = str(line).split(",")
        if len(parts) < 6:
            continue
        close = to_float(parts[2])
        if close is None or not parts[0]:
            continue
        rows.append({
            "date": parts[0],
            "open": to_float(parts[1]),
            "close": close,
            "high": to_float(parts[3]),
            "low": to_float(parts[4]),
            "volume": to_float(parts[5]),
            "source": "eastmoney_kline",
            "adjust": adjust,
            "fqt": str(fqt),
            "code": code,
        })
    return rows


def parse_sina_kline(payload, code: str) -> list[dict]:
    """Sina getKLineData bars. Adjustment is not declared by this endpoint."""
    if not isinstance(payload, list):
        return []
    rows = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        close = to_float(item.get("close"))
        if close is None:
            continue
        rows.append({
            "date": item.get("day"),
            "open": to_float(item.get("open")),
            "close": close,
            "high": to_float(item.get("high")),
            "low": to_float(item.get("low")),
            "volume": to_float(item.get("volume")),
            "source": "sina_kline",
            "adjust": "unknown",
        })
    return rows


def parse_ifzq_kline(payload: dict, code: str) -> list[dict]:
    code = normalize_code(code)
    data = (payload or {}).get("data") or {}
    stock = None
    for key, value in data.items():
        if key.endswith(code) and isinstance(value, dict):
            stock = value
            break
    if not stock:
        return []
    raw_rows = stock.get("qfqday") or stock.get("day") or []
    adjust = "qfq" if stock.get("qfqday") else "raw"
    parsed = []
    for item in raw_rows:
        if not item or len(item) < 6:
            continue
        parsed.append(
            {
                "date": item[0],
                "open": to_float(item[1]),
                "close": to_float(item[2]),
                "high": to_float(item[3]),
                "low": to_float(item[4]),
                "volume": to_float(item[5]),
                "source": "ifzq_gtimg",
                "adjust": adjust,
            }
        )
    return parsed


def parse_jsonp(text: str) -> Any:
    raw = text.strip()
    start = raw.find("(")
    end = raw.rfind(")")
    if start < 0 or end <= start:
        raise ValueError("响应不是 JSONP")
    return json.loads(raw[start + 1 : end])


def provenance_banner(provenance: str) -> str:
    if provenance == PROVENANCE_LIVE:
        return "LIVE — 本次进程直接请求公开只读接口"
    if provenance == PROVENANCE_SYNTHETIC:
        return "SYNTHETIC_ONLY — 人工夹具，不是行情，不能当作真实筛选或报告"
    if provenance == PROVENANCE_RECORDED:
        return "RECORDED_LIVE — 冻结的历史响应，不是本次现拉"
    if provenance == PROVENANCE_INJECTED:
        return "INJECTED — 调用方注入的传输层，不是现拉"
    return f"UNKNOWN_PROVENANCE — {provenance}"
