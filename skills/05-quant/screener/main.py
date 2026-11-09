#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""screener: 条件筛选。

目录名含数字和连字符，不能写成 ``import skills.01-infra...``。
本模块用 importlib 按文件路径加载数据源和缓存。

网络默认有上限：指定代码最多 1 次批量行情；不指定代码时只取一页
（默认 80 只），并标明这不是全市场。失败时返回空结果和错误，不编造行情。
"""

import argparse
import json
import math
import re
import sys
import warnings
from pathlib import Path
from typing import Dict, List, Optional

warnings.filterwarnings("ignore")

_REPO = Path(__file__).resolve()
for _candidate in _REPO.parents:
    if (_candidate / "astock_runtime.py").is_file() and (_candidate / "skills").is_dir():
        if str(_candidate) not in sys.path:
            sys.path.insert(0, str(_candidate))
        _REPO = _candidate
        break
else:
    raise RuntimeError("找不到仓库根目录")

import astock_runtime

import pandas as pd

PROGRESS_FILE = Path("./screener_progress.json")

BUILTIN_STRATEGIES = {
    "value": {
        "name": "价值低估",
        "where": "pe<20 and pb<2 and total_mv>50",
        "sort": "pe",
    },
    "growth": {
        "name": "成长",
        "where": "roe>15 and pe>10 and pe<50",
        "sort": "roe",
    },
    "small_cap": {
        "name": "小市值",
        "where": "total_mv<100 and total_mv>20",
        "sort": "total_mv",
    },
    "momentum": {
        "name": "动量",
        "where": "change_5d>5 and change_20d>0",
        "sort": "change_5d",
    },
}

ALLOWED_FIELDS = {
    "code", "name", "price", "pct_change", "change",
    "volume", "amount", "turnover", "pe", "pb", "ps",
    "total_mv", "circ_mv", "change_5d", "change_20d", "roe",
}
_KEYWORDS = {"and", "or", "not", "True", "False", "None"}
_NAME_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\b")
_BETWEEN_RE = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_]*)\s+between\s+(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\b",
    re.IGNORECASE,
)
_FIELD_RE = {
    "change_5d": re.compile(r"\bchange_5d\b"),
    "change_20d": re.compile(r"\bchange_20d\b"),
}

SIGNAL_NAMES = {
    "ma_cross",
    "macd_golden",
    "macd_death",
    "above_ma20",
    "above_ma60",
    "volume_break",
    "volume_shrink",
    "rsi_oversold",
    "rsi_overbought",
    "kdj_golden",
    "new_high_60",
    "limit_up",
}


_LOADED = {}


def _load(rel: str, module_name: str):
    if module_name not in _LOADED:
        _LOADED[module_name] = astock_runtime.load_py(_REPO / rel, module_name)
    return _LOADED[module_name]


def _data_source():
    return _load("skills/01-infra/astock-data-source/main.py", "astock_data_source_main")


def _cache():
    return _load("skills/01-infra/astock-cache/main.py", "astock_cache_main")


def load_fixture(path: str):
    """Load an offline universe. The file must declare provenance=synthetic."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    provenance = payload.get("provenance")
    if provenance != astock_runtime.PROVENANCE_SYNTHETIC:
        raise ValueError(
            "夹具 provenance 必须是 synthetic。拒绝把文件中的 live/recorded 标成现拉结果。"
        )
    rows = payload.get("rows")
    if not isinstance(rows, list) or not rows:
        raise ValueError("夹具 rows 必须是非空列表")
    frame = pd.DataFrame(rows)
    klines = {}
    for code, bars in (payload.get("klines") or {}).items():
        klines[astock_runtime.normalize_code(code)] = pd.DataFrame(bars)
    return frame, klines, payload.get("fixture_id")


def _rewrite_where(expr: str) -> str:
    rewritten = _BETWEEN_RE.sub(
        lambda match: (
            f"({match.group(1)} >= {match.group(2)} and {match.group(1)} <= {match.group(3)})"
        ),
        expr,
    )
    if re.search(r"[A-Za-z_][A-Za-z0-9_]*\s*\(", rewritten):
        raise ValueError("条件不允许函数调用")
    if any(token in rewritten for token in ("__", "lambda", "import", "[", "]")):
        raise ValueError("条件含不允许的语法")
    names = set(_NAME_RE.findall(rewritten))
    unknown = names - ALLOWED_FIELDS - _KEYWORDS
    if unknown:
        raise ValueError(f"条件含未允许字段: {', '.join(sorted(unknown))}")
    compile(rewritten, "<where>", "eval")
    return rewritten


def conditions_to_where(conditions: List[dict], mode: str = "and") -> str:
    """Compile the documented condition objects into one expression."""
    if mode not in {"and", "or"}:
        raise ValueError("mode 只能是 and 或 or")
    parts = []
    for item in conditions:
        field = item.get("field")
        op = item.get("op")
        value = item.get("value")
        if field not in ALLOWED_FIELDS:
            raise ValueError(f"不允许的字段: {field}")
        if op == "between":
            if not isinstance(value, (list, tuple)) or len(value) != 2:
                raise ValueError("between 需要 [low, high]")
            low, high = float(value[0]), float(value[1])
            parts.append(f"({field} >= {low} and {field} <= {high})")
        elif op in {">", "<", ">=", "<=", "==", "!="}:
            parts.append(f"({field} {op} {float(value)})")
        else:
            raise ValueError(f"不支持的操作符: {op}")
    joiner = " and " if mode == "and" else " or "
    return joiner.join(parts)


def safe_eval_condition(row, expr: str):
    """True or False when every field is present. None when a value is missing.

    Missing values are not coerced to 0 and are not treated as a failed comparison.
    """
    rewritten = _rewrite_where(expr)
    used = [name for name in _NAME_RE.findall(rewritten) if name in ALLOWED_FIELDS]
    ctx = {}
    for field in used:
        value = row.get(field) if hasattr(row, "get") else row[field]
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return None
        try:
            if pd.isna(value):
                return None
        except TypeError:
            pass
        if isinstance(value, str) and field in {"code", "name"}:
            ctx[field] = value
            continue
        try:
            ctx[field] = float(value)
        except (TypeError, ValueError):
            return None
    ctx["True"] = True
    ctx["False"] = False
    ctx["None"] = None
    return bool(eval(rewritten, {"__builtins__": {}}, ctx))


def _blank_frame(meta: dict) -> pd.DataFrame:
    frame = pd.DataFrame()
    frame.attrs.update(meta)
    return frame


def _attach(frame: pd.DataFrame, meta: dict) -> pd.DataFrame:
    frame = frame.copy()
    frame.attrs.update(meta)
    return frame


def _prepare_ohlcv(frame: pd.DataFrame) -> pd.DataFrame:
    data = frame.copy()
    data.columns = [str(col).lower() for col in data.columns]
    rename = {"收盘": "close", "开盘": "open", "最高": "high", "最低": "low", "成交量": "volume"}
    return data.rename(columns={key: value for key, value in rename.items() if key in data.columns})


def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def signal_status(frame: pd.DataFrame, name: str):
    """True, False, or None when the bar count cannot support the signal.

    ``limit_up`` uses previous-close change >= 9.9. It is not an exchange
    limit-price check (STAR/ChiNext/ST bands differ).
    ``ma_cross`` is MA5 crossing above MA10 on the last bar.
    """
    if name not in SIGNAL_NAMES:
        raise ValueError(f"未知信号: {name}")
    if frame is None or frame.empty:
        return None
    data = _prepare_ohlcv(frame)
    if "close" not in data.columns:
        return None
    close = pd.to_numeric(data["close"], errors="coerce")
    if close.dropna().empty:
        return None

    def crossed_above(fast, slow) -> Optional[bool]:
        if len(close) < 2 or pd.isna(fast.iloc[-1]) or pd.isna(slow.iloc[-1]):
            return None
        if pd.isna(fast.iloc[-2]) or pd.isna(slow.iloc[-2]):
            return None
        return bool(fast.iloc[-1] > slow.iloc[-1] and fast.iloc[-2] <= slow.iloc[-2])

    if name == "above_ma20":
        if len(close) < 20:
            return None
        ma = close.rolling(20).mean().iloc[-1]
        return bool(close.iloc[-1] > ma)
    if name == "above_ma60":
        if len(close) < 60:
            return None
        ma = close.rolling(60).mean().iloc[-1]
        return bool(close.iloc[-1] > ma)
    if name == "ma_cross":
        if len(close) < 10:
            return None
        return crossed_above(close.rolling(5).mean(), close.rolling(10).mean())
    if name in {"macd_golden", "macd_death"}:
        if len(close) < 35:
            return None
        dif = _ema(close, 12) - _ema(close, 26)
        dea = _ema(dif, 9)
        if name == "macd_golden":
            return crossed_above(dif, dea)
        return crossed_above(dea, dif)
    if name in {"volume_break", "volume_shrink"}:
        if "volume" not in data.columns or len(data) < 6:
            return None
        volume = pd.to_numeric(data["volume"], errors="coerce")
        base = volume.iloc[-6:-1].mean()
        if base is None or pd.isna(base) or base == 0 or pd.isna(volume.iloc[-1]):
            return None
        ratio = float(volume.iloc[-1] / base)
        return bool(ratio > 1.5) if name == "volume_break" else bool(ratio < 0.5)
    if name in {"rsi_oversold", "rsi_overbought"}:
        if len(close) < 7:
            return None
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(6).mean()
        loss = (-delta.clip(upper=0)).rolling(6).mean()
        rs = gain / loss.replace(0, pd.NA)
        rsi = 100 - (100 / (1 + rs))
        last = rsi.iloc[-1]
        if pd.isna(last):
            return None
        return bool(last < 20) if name == "rsi_oversold" else bool(last > 80)
    if name == "kdj_golden":
        if not {"high", "low"}.issubset(data.columns) or len(data) < 12:
            return None
        high = pd.to_numeric(data["high"], errors="coerce")
        low = pd.to_numeric(data["low"], errors="coerce")
        span = high.rolling(9).max() - low.rolling(9).min()
        rsv = (close - low.rolling(9).min()) / span.replace(0, pd.NA) * 100
        k = rsv.ewm(alpha=1 / 3, adjust=False).mean()
        d = k.ewm(alpha=1 / 3, adjust=False).mean()
        return crossed_above(k, d)
    if name == "new_high_60":
        if len(close) < 60:
            return None
        window = close.iloc[-60:]
        if window.isna().any():
            return None
        return bool(window.iloc[-1] >= window.max())
    if name == "limit_up":
        if len(close) < 2 or pd.isna(close.iloc[-2]) or close.iloc[-2] == 0:
            return None
        pct = (close.iloc[-1] - close.iloc[-2]) / close.iloc[-2] * 100
        return bool(pct >= 9.9)
    return None


def _needs_change(where: Optional[str], column: str, frame: pd.DataFrame) -> bool:
    if not where or column in frame.columns:
        return False
    return _FIELD_RE[column].search(where) is not None


def _fetch_kline_frame(code: str, days: int, timeout: float) -> pd.DataFrame:
    response = astock_runtime.http_get(
        astock_runtime.IFZQ_URL,
        params={"param": f"{astock_runtime.sina_symbol(code)},day,,,{int(days)},qfq"},
        timeout=timeout,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    response.raise_for_status()
    rows = astock_runtime.parse_ifzq_kline(response.json(), code)
    frame = pd.DataFrame(rows)
    frame.attrs["source"] = "ifzq_gtimg"
    frame.attrs["provenance"] = astock_runtime.PROVENANCE_LIVE
    return frame


def _kline_for_code(code: str, days: int, timeout: float, klines: Optional[dict], meta: dict):
    if klines is not None:
        frame = klines.get(code)
        if frame is None:
            meta["skipped"].append({"code": code, "reason": "fixture_kline_missing"})
            return None
        return frame
    try:
        cache = _cache()
        frame = cache.kline_get_or_fetch(
            code,
            lambda c, d: _fetch_kline_frame(c, d, timeout),
            days=days,
        )
    except Exception as exc:
        meta["errors"].append(f"kline {code}: {type(exc).__name__}: {exc}")
        meta["skipped"].append({"code": code, "reason": "kline_fetch_failed"})
        return None
    if frame is None or frame.empty:
        meta["skipped"].append({"code": code, "reason": "kline_empty"})
        return None
    return frame


def _apply_change(frame: pd.DataFrame, where: str, days: int, timeout: float,
                  klines: Optional[dict], meta: dict, limit: int) -> pd.DataFrame:
    need5 = _needs_change(where, "change_5d", frame)
    need20 = _needs_change(where, "change_20d", frame)
    if not need5 and not need20:
        return frame
    codes = frame["code"].astype(str).tolist() if "code" in frame.columns else []
    if len(codes) > limit:
        extra = codes[limit:]
        codes = codes[:limit]
        meta["skipped"].extend({"code": code, "reason": "change_fetch_budget"} for code in extra)
        meta["note"] = (meta.get("note") or "") + f" 涨幅只计算前 {limit} 只，其余因预算未计算。"
    change5, change20 = {}, {}
    progress = {}
    for code in codes:
        bars = _kline_for_code(code, days, timeout, klines, meta)
        if bars is None:
            continue
        data = _prepare_ohlcv(bars)
        if "close" not in data.columns:
            meta["skipped"].append({"code": code, "reason": "kline_missing_close"})
            continue
        close = pd.to_numeric(data["close"], errors="coerce").dropna()
        if len(close) < 6:
            meta["skipped"].append({"code": code, "reason": "kline_shorter_than_6"})
            continue
        last = float(close.iloc[-1])
        if need5 and float(close.iloc[-6]) != 0:
            change5[code] = (last - float(close.iloc[-6])) / float(close.iloc[-6]) * 100
        if need20:
            if len(close) < 21:
                meta["skipped"].append({"code": code, "reason": "kline_shorter_than_21"})
            elif float(close.iloc[-21]) != 0:
                change20[code] = (last - float(close.iloc[-21])) / float(close.iloc[-21]) * 100
        progress[code] = {"change_5d": change5.get(code), "change_20d": change20.get(code)}
    if klines is None and progress:
        try:
            PROGRESS_FILE.write_text(json.dumps({"done": progress}, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:
            meta["errors"].append(f"进度保存失败: {exc}")
    if need5:
        frame["change_5d"] = frame["code"].map(change5)
    if need20:
        frame["change_20d"] = frame["code"].map(change20)
    return frame


def _fetch_live_quotes(scope: Optional[List[str]], universe_limit: int, timeout: float, meta: dict):
    source = _data_source()
    if scope:
        if len(scope) > 50:
            raise ValueError("一次最多筛选 50 只代码。未自动翻页或扩大到全市场。")
        try:
            frame = source.get_quotes_for_codes(scope, timeout=timeout)
            meta["source"] = "eastmoney_ulist"
            meta["missing"] = list(frame.attrs.get("missing") or [])
            meta["universe_complete"] = bool(frame.attrs.get("universe_complete"))
            meta["fetch_invocations"] = 1
            return frame
        except Exception as exc:
            meta["errors"].append(f"eastmoney_ulist: {exc}")
            symbols = ",".join(astock_runtime.sina_symbol(code) for code in scope)
            try:
                response = astock_runtime.http_get(
                    astock_runtime.TENCENT_QT_URL + symbols,
                    timeout=timeout,
                    encoding="gbk",
                    headers={"User-Agent": "Mozilla/5.0"},
                )
                response.raise_for_status()
                rows = astock_runtime.parse_tencent_quotes(response.text, include_fundamentals=True)
                if rows:
                    frame = pd.DataFrame(rows)
                    got = set(frame["code"].tolist())
                    meta["missing"] = [code for code in scope if code not in got]
                    meta["source"] = "tencent_qt"
                    meta["fallback"] = True
                    meta["universe_complete"] = not meta["missing"]
                    meta["fetch_invocations"] = 2
                    meta["note"] = (
                        "东财 ulist 失败，改用腾讯 qt.gtimg.cn。"
                        "pe 是字段 52（与东财动态市盈率对齐过），不是字段 39。"
                        "total_mv 为亿元。没有的代码不补价。"
                    )
                    return frame
                meta["errors"].append("tencent_qt: 没有解析出行")
            except Exception as exc2:
                meta["errors"].append(f"tencent_qt: {type(exc2).__name__}: {exc2}")
            response = astock_runtime.http_get(
                astock_runtime.SINA_HQ_URL + symbols,
                timeout=timeout,
                encoding="gb18030",
                headers={"Referer": "https://finance.sina.com.cn", "User-Agent": "Mozilla/5.0"},
            )
            response.raise_for_status()
            rows = astock_runtime.parse_sina_hq_batch(response.text)
            frame = pd.DataFrame(rows)
            got = set(frame["code"].tolist()) if not frame.empty else set()
            meta["missing"] = [code for code in scope if code not in got]
            meta["source"] = "sina_hq"
            meta["fallback"] = True
            meta["universe_complete"] = not meta["missing"]
            meta["fetch_invocations"] = 3
            meta["note"] = (
                "东财 ulist 和腾讯 qt 都没有给出可用行情，改用新浪个股行情。"
                "该源没有 PE/PB/总市值，这些字段保持为空，不会用估算值填上。"
            )
            return frame
    frame = source.get_realtime_all(num=universe_limit, timeout=max(timeout, 12))
    meta["source"] = frame.attrs.get("source")
    meta["universe_complete"] = False
    meta["requested_num"] = frame.attrs.get("requested_num")
    meta["fetch_invocations"] = 1
    meta["note"] = frame.attrs.get("note") or ""
    return frame


def screen(where: str = None, signals: List[str] = None,
           sort: str = None, top_n: int = 30,
           scope: List[str] = None, kline_days: int = 60,
           quotes: pd.DataFrame = None, klines: dict = None,
           provenance: str = None, universe_limit: int = 80,
           signal_limit: int = 10, timeout: float = 8.0,
           conditions: List[dict] = None, mode: str = "and") -> pd.DataFrame:
    """Screen a quote table.

    Passing ``quotes`` forces a non-live provenance. Live is only set when this
    function performs the fetch itself.
    """
    meta = {
        "errors": [],
        "skipped": [],
        "missing": [],
        "missing_fields": [],
        "fallback": False,
        "fetch_invocations": 0,
        "signal_definitions": {
            "limit_up": "前收涨幅>=9.9，不是交易所涨停价",
            "ma_cross": "MA5 上穿 MA10",
        },
    }
    if conditions:
        compiled = conditions_to_where(conditions, mode=mode)
        where = f"({where}) and ({compiled})" if where else compiled
    if signals:
        unknown = [name for name in signals if name not in SIGNAL_NAMES]
        if unknown:
            raise ValueError(f"未知信号: {', '.join(unknown)}")
        if mode not in {"and", "or"}:
            raise ValueError("mode 只能是 and 或 or")
    if where:
        where = _rewrite_where(where)

    if quotes is not None:
        if provenance is None:
            provenance = astock_runtime.PROVENANCE_SYNTHETIC
        if provenance == astock_runtime.PROVENANCE_LIVE:
            raise ValueError("传入的行情表不能标记为 live")
        if provenance not in astock_runtime.ALLOWED_PROVENANCE:
            raise ValueError(f"未知 provenance: {provenance}")
        frame = quotes.copy()
        meta["source"] = "caller_quotes"
        meta["universe_complete"] = True
    else:
        if provenance not in (None, astock_runtime.PROVENANCE_LIVE):
            raise ValueError("只有实际请求才能标记 live")
        provenance = astock_runtime.PROVENANCE_LIVE
        try:
            frame = _fetch_live_quotes(scope, universe_limit, timeout, meta)
        except Exception as exc:
            meta["errors"].append(f"{type(exc).__name__}: {exc}")
            meta["provenance"] = provenance
            meta["note"] = (meta.get("note") or "") + " 行情请求失败，未使用替代数据。"
            return _blank_frame(meta)

    meta["provenance"] = provenance
    if frame is None or frame.empty:
        meta["note"] = (meta.get("note") or "") + " 行情为空。"
        return _blank_frame(meta)

    if "code" in frame.columns:
        frame["code"] = frame["code"].astype(str).str.replace(r"\D", "", regex=True).str.zfill(6)
    if scope and "code" in frame.columns:
        wanted = [astock_runtime.normalize_code(code) for code in scope]
        meta["requested"] = wanted
        frame = frame[frame["code"].isin(wanted)].copy()
        present = set(frame["code"].tolist())
        meta["missing"] = list(dict.fromkeys(
            list(meta.get("missing") or []) + [code for code in wanted if code not in present]
        ))
    if "name" in frame.columns:
        st_mask = frame["name"].astype(str).str.contains("ST", na=False)
        meta["excluded_st"] = frame.loc[st_mask, "code"].astype(str).tolist() if "code" in frame.columns else []
        frame = frame[~st_mask].copy()

    if where and (_needs_change(where, "change_5d", frame) or _needs_change(where, "change_20d", frame)):
        if provenance != astock_runtime.PROVENANCE_LIVE and klines is None:
            meta["errors"].append("非 live 行情缺少 change_5d/change_20d，且没有夹具 K 线。未发网络请求。")
        else:
            frame = _apply_change(frame, where, kline_days, timeout, klines, meta, signal_limit)

    if where:
        used = [name for name in _NAME_RE.findall(where) if name in ALLOWED_FIELDS]
        absent = [name for name in used if name not in frame.columns]
        meta["missing_fields"] = absent
        decision = frame.apply(lambda row: safe_eval_condition(row, where), axis=1)
        if "code" in frame.columns:
            meta["not_evaluated"] = frame.loc[decision.isna(), "code"].astype(str).tolist()
        else:
            meta["not_evaluated"] = []
        frame = frame.loc[decision.eq(True)].copy()

    if signals:
        if "code" not in frame.columns:
            meta["errors"].append("没有 code 列，无法计算信号")
            frame = frame.iloc[0:0].copy()
        else:
            codes = frame["code"].astype(str).tolist()
            if len(codes) > signal_limit:
                meta["skipped"].extend(
                    {"code": code, "reason": "signal_fetch_budget"} for code in codes[signal_limit:]
                )
                codes = codes[:signal_limit]
            triggered = {}
            for code in codes:
                bars = _kline_for_code(code, kline_days, timeout, klines, meta)
                flags = []
                if bars is None:
                    flags = [None] * len(signals)
                else:
                    for name in signals:
                        flags.append(signal_status(bars, name))
                triggered[code] = flags
            if mode == "and":
                keep = [
                    code for code, flags in triggered.items()
                    if flags and all(flag is True for flag in flags)
                ]
            else:
                keep = [
                    code for code, flags in triggered.items()
                    if any(flag is True for flag in flags)
                ]
            unevaluated = [
                code for code, flags in triggered.items() if any(flag is None for flag in flags)
            ]
            if unevaluated:
                meta["skipped"].extend(
                    {"code": code, "reason": "signal_not_evaluated"} for code in unevaluated
                )
            frame = frame[frame["code"].isin(keep)].copy()

    if sort:
        if sort not in frame.columns:
            meta["errors"].append(f"排序字段不存在: {sort}")
        else:
            frame = frame.sort_values(sort, ascending=True)
    elif "pct_change" in frame.columns:
        frame = frame.sort_values("pct_change", ascending=False)

    result = frame.head(top_n).reset_index(drop=True)
    requested = meta.get("requested") or []
    kept = set(result["code"].astype(str)) if "code" in result.columns else set()
    missing = set(meta.get("missing") or [])
    not_evaluated = set(meta.get("not_evaluated") or [])
    meta["filtered_out"] = [
        code for code in requested
        if code not in kept and code not in missing and code not in not_evaluated
    ]
    if klines is None and PROGRESS_FILE.exists() and meta.get("fetch_invocations"):
        try:
            PROGRESS_FILE.unlink()
        except Exception:
            pass
    return _attach(result, meta)


def result_payload(frame: pd.DataFrame) -> dict:
    rows = frame.to_dict(orient="records") if frame is not None and not frame.empty else []
    return {
        "provenance": frame.attrs.get("provenance"),
        "banner": astock_runtime.provenance_banner(frame.attrs.get("provenance") or ""),
        "source": frame.attrs.get("source"),
        "universe_complete": frame.attrs.get("universe_complete"),
        "fallback": frame.attrs.get("fallback"),
        "requested": frame.attrs.get("requested") or [],
        "filtered_out": frame.attrs.get("filtered_out") or [],
        "not_evaluated": frame.attrs.get("not_evaluated") or [],
        "missing": frame.attrs.get("missing") or [],
        "missing_fields": frame.attrs.get("missing_fields") or [],
        "excluded_st": frame.attrs.get("excluded_st") or [],
        "skipped": frame.attrs.get("skipped") or [],
        "errors": frame.attrs.get("errors") or [],
        "note": frame.attrs.get("note") or "",
        "row_count": len(rows),
        "rows": rows,
    }


def _print_table(frame: pd.DataFrame) -> None:
    payload = result_payload(frame)
    print(payload["banner"])
    if payload["source"]:
        print(f"source={payload['source']} universe_complete={payload['universe_complete']}")
    if payload["note"]:
        print(payload["note"])
    if payload["missing"]:
        print("缺失代码（未补价）: " + ",".join(payload["missing"]))
    if payload["missing_fields"]:
        print("条件字段在行情中不存在，对应行不会命中: " + ",".join(payload["missing_fields"]))
    if payload["errors"]:
        print("错误: " + " | ".join(str(item) for item in payload["errors"]))
    if payload["skipped"]:
        print(f"未评估 {len(payload['skipped'])} 项（见 --json）")
    if frame.empty:
        if payload["errors"]:
            print("无结果。未使用替代数据。")
        else:
            print("无符合条件")
        return
    columns = [
        "code", "name", "price", "pct_change", "pe", "pb", "total_mv",
        "turnover", "change_5d", "change_20d",
    ]
    columns = [column for column in columns if column in frame.columns]
    with pd.option_context("display.max_rows", 100, "display.width", 200,
                            "display.float_format", "{:.2f}".format):
        print(frame[columns].to_string(index=False))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="screener（有界行情 + 离线夹具）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python main.py screen --where "pe<20" --codes 600519,000001
  python main.py screen --strategy value --universe-limit 80
  python main.py screen --fixture tests/fixtures/screener_universe_synthetic.json --where "pe<20"
  python main.py list
        """,
    )
    sub = parser.add_subparsers(dest="cmd")

    screen_parser = sub.add_parser("screen", help="筛选")
    screen_parser.add_argument("--where", help='表达式, 如 "pe<20 and total_mv>50"')
    screen_parser.add_argument("--signals", help="信号, 逗号分隔")
    screen_parser.add_argument("--strategy", help="value/growth/small_cap/momentum")
    screen_parser.add_argument("--sort", help="排序字段")
    screen_parser.add_argument("--top", type=int, default=30)
    screen_parser.add_argument("--codes", help="股票池, 逗号分隔, 最多 50")
    screen_parser.add_argument("--kline-days", type=int, default=60)
    screen_parser.add_argument("--universe-limit", type=int, default=80,
                               help="未指定 --codes 时的单页条数。这不是全市场")
    screen_parser.add_argument("--signal-limit", type=int, default=10,
                               help="信号或涨幅计算最多覆盖多少只")
    screen_parser.add_argument("--timeout", type=float, default=8.0)
    screen_parser.add_argument("--mode", choices=["and", "or"], default="and")
    screen_parser.add_argument("--fixture", help="synthetic JSON 夹具。结果不会标成 live")
    screen_parser.add_argument("--json", action="store_true")

    sub.add_parser("list", help="列出内置策略")
    show_parser = sub.add_parser("show", help="显示策略详情")
    show_parser.add_argument("name")
    sub.add_parser("cache-stats", help="K线缓存统计")

    args = parser.parse_args(argv)

    if args.cmd == "list":
        print("内置策略:")
        for key, value in BUILTIN_STRATEGIES.items():
            print(f"  {key}: {value['name']} --where {value['where']!r}")
        print("未指定 --codes 时默认只取一页行情，并标明 universe_complete=false。")
        return 0
    if args.cmd == "show":
        if args.name not in BUILTIN_STRATEGIES:
            print(f"未找到策略: {args.name}")
            return 2
        print(json.dumps(BUILTIN_STRATEGIES[args.name], ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "cache-stats":
        print(json.dumps(_cache().kline_stats(), ensure_ascii=False))
        return 0
    if args.cmd != "screen":
        parser.print_help()
        return 0

    where = args.where
    sort = args.sort
    if args.strategy:
        if args.strategy not in BUILTIN_STRATEGIES:
            print(f"未知策略: {args.strategy}")
            print("可用: " + ", ".join(BUILTIN_STRATEGIES.keys()))
            return 2
        chosen = BUILTIN_STRATEGIES[args.strategy]
        where = args.where or chosen.get("where")
        sort = args.sort or chosen.get("sort")
    if not where and not args.signals:
        print("请提供 --where 或 --signals 或 --strategy")
        return 2

    quotes = None
    klines = None
    provenance = None
    try:
        if args.fixture:
            quotes, klines, fixture_id = load_fixture(args.fixture)
            provenance = astock_runtime.PROVENANCE_SYNTHETIC
            quotes.attrs["fixture_id"] = fixture_id
        codes = None
        if args.codes:
            codes = [astock_runtime.normalize_code(item) for item in args.codes.split(",") if item.strip()]
        result = screen(
            where=where,
            signals=args.signals.split(",") if args.signals else None,
            sort=sort,
            top_n=args.top,
            scope=codes,
            kline_days=args.kline_days,
            quotes=quotes,
            klines=klines,
            provenance=provenance,
            universe_limit=args.universe_limit,
            signal_limit=args.signal_limit,
            timeout=args.timeout,
            mode=args.mode,
        )
    except ValueError as exc:
        print(f"参数错误: {exc}")
        return 2

    payload = result_payload(result)
    if args.fixture:
        payload["fixture_id"] = quotes.attrs.get("fixture_id")
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        _print_table(result)
    if payload.get("not_evaluated") and payload["row_count"] == 0:
        return 1
    if payload["errors"] and payload["row_count"] == 0 and not payload.get("filtered_out"):
        return 1
    if payload.get("not_evaluated"):
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
