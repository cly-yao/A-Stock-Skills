#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""report: 每日复盘 / 个股 / 持仓。

默认路径不再调用 akshare 的全市场分页接口。每次报告有 HTTP 次数上限
和单次超时。某一节失败时写入失败原因，不用空表或估算值冒充该节数据。
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

_REPO = Path(__file__).resolve()
for _candidate in _REPO.parents:
    if (_candidate / "astock_runtime.py").is_file() and (_candidate / "skills").is_dir():
        if str(_candidate) not in sys.path:
            sys.path.insert(0, str(_candidate))
        break
else:
    raise RuntimeError("找不到仓库根目录")

import astock_runtime


def fmt_money(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "-"
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "-"
    sign = "+" if value > 0 else ""
    if abs(value) >= 1e8:
        return f"{sign}{value / 1e8:.2f}亿"
    if abs(value) >= 1e4:
        return f"{sign}{value / 1e4:.2f}万"
    return f"{value:+.0f}"


def fmt_pct(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "-"
    try:
        return f"{float(value):+.2f}%"
    except (TypeError, ValueError):
        return "-"


def fmt_num(value, digits=2):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "-"
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "-"


def _resolve_transport(transport, provenance, max_calls, timeout):
    if transport is None:
        if provenance not in (None, astock_runtime.PROVENANCE_LIVE):
            raise ValueError("默认网络传输只能标记 live")
        return astock_runtime.PROVENANCE_LIVE, astock_runtime.HttpBudget(max_calls, timeout)
    if provenance is None:
        provenance = getattr(transport, "provenance", None) or astock_runtime.PROVENANCE_SYNTHETIC
    if provenance == astock_runtime.PROVENANCE_LIVE:
        raise ValueError("注入的 transport 不能标记为 live")
    if provenance not in astock_runtime.ALLOWED_PROVENANCE:
        raise ValueError(f"未知 provenance: {provenance}")
    return provenance, transport


def _section(section_id, title, status, source=None, rows=None, note=None, error=None, extra=None):
    item = {
        "id": section_id,
        "title": title,
        "status": status,
        "source": source,
        "rows": rows or [],
        "note": note,
        "error": error,
    }
    if extra:
        item.update(extra)
    return item


def _call(transport, url, params=None, encoding=None, headers=None):
    return transport.request("GET", url, params=params, encoding=encoding, headers=headers)


def _guard(section_id, title, fn):
    try:
        return fn()
    except astock_runtime.BudgetExceeded as exc:
        return _section(section_id, title, "skipped", error=str(exc),
                        note="预算用尽，本节未请求，未填入替代数据。")
    except Exception as exc:
        return _section(section_id, title, "failed", error=f"{type(exc).__name__}: {exc}",
                        note="本节失败，未填入替代数据。")


def _section_indices(transport):
    def run():
        symbols = ",".join(astock_runtime.MAJOR_INDEX_SINA)
        response = _call(
            transport,
            astock_runtime.SINA_HQ_URL + symbols,
            encoding="gb18030",
            headers={"Referer": "https://finance.sina.com.cn", "User-Agent": "Mozilla/5.0"},
        )
        rows = astock_runtime.parse_sina_index_quotes(response.text)
        got = {row["symbol"] for row in rows}
        missing = [symbol for symbol in astock_runtime.MAJOR_INDEX_SINA if symbol not in got]
        status = "ok" if rows and not missing else ("partial" if rows else "failed")
        error = None if rows else "指数接口没有返回可解析行情"
        return _section(
            "indices", "主要指数", status, source="sina_index_s", rows=rows,
            note="新浪简版指数行情，一次请求。",
            error=error,
            extra={"missing_symbols": missing},
        )
    return _guard("indices", "主要指数", run)


def _section_breadth(transport):
    def run():
        response = _call(
            transport,
            astock_runtime.EM_FENBU_URL,
            params={"ut": "7eea3edcaed734bea9cbfc24409ed989", "dpt": "wz.ztzt"},
        )
        payload = response.json()
        data = (payload or {}).get("data") or {}
        buckets = data.get("fenbu")
        if not isinstance(buckets, list) or not buckets:
            return _section("breadth", "市场广度", "failed", source="eastmoney_zdfenbu",
                            error="fenbu 为空", note="未把空分布写成上涨 0 家。")
        up = down = flat = 0
        parsed = []
        for item in buckets:
            if not isinstance(item, dict) or len(item) != 1:
                continue
            key, count = next(iter(item.items()))
            bucket = int(key)
            count = int(count)
            parsed.append({"bucket": bucket, "count": count})
            if bucket > 0:
                up += count
            elif bucket < 0:
                down += count
            else:
                flat += count
        return _section(
            "breadth", "市场广度", "ok", source="eastmoney_zdfenbu",
            rows=parsed,
            note=(
                "来源涨跌分布整数桶。正桶合计记为上涨，负桶合计记为下跌，0 为平盘。"
                "这不是逐只股票核对后的交易所官方家数，也不能从桶值 10 推断涨停。"
            ),
            extra={"qdate": data.get("qdate"), "up": up, "down": down, "flat": flat,
                   "counted": up + down + flat},
        )
    return _guard("breadth", "市场广度", run)


def _section_industry(transport):
    def run():
        response = _call(
            transport,
            astock_runtime.EM_CLIST_URL,
            params={
                "pn": "1",
                "pz": "5",
                "po": "1",
                "np": "1",
                "fltt": "2",
                "invt": "2",
                "fid": "f3",
                "fs": "m:90+t:2+f:!50",
                "fields": "f12,f14,f3,f128,f140",
            },
        )
        payload = response.json()
        data = (payload or {}).get("data") or {}
        diff = data.get("diff") or []
        if not isinstance(diff, list) or not diff:
            return _section("industry", "行业板块", "failed", source="eastmoney_industry_clist",
                            error="板块列表为空", note="未改用其他板块口径填充。")
        rows = []
        for item in diff:
            rows.append({
                "code": item.get("f12"),
                "name": item.get("f14"),
                "pct_change": astock_runtime.to_float(item.get("f3")),
                "leader": item.get("f128"),
                "leader_code": item.get("f140"),
            })
        return _section(
            "industry", "行业板块 Top 5", "ok", source="eastmoney_industry_clist",
            rows=rows,
            note=f"服务端按涨跌幅降序返回的前 {len(rows)} 名。板块总数 {data.get('total')}。未翻页。",
            extra={"board_total": data.get("total"), "page_size": 5},
        )
    return _guard("industry", "行业板块 Top 5", run)


def _section_limit_up(transport, date: Optional[str]):
    def run():
        params = {
            "ut": "7eea3edcaed734bea9cbfc24409ed989",
            "dpt": "wz.ztzt",
            "Pageindex": "0",
            "pagesize": "5",
            "sort": "fbt:asc",
        }
        if date:
            params["date"] = date.replace("-", "")
        response = _call(transport, astock_runtime.EM_ZT_URL, params=params)
        payload = response.json()
        if not isinstance(payload, dict):
            return _section(
                "limit_up", "涨停", "failed", source="eastmoney_zt_pool",
                error=f"响应不是对象: {type(payload).__name__}",
                note="未把失败写成涨停 0 家。",
            )
        data = payload.get("data")
        if not isinstance(data, dict):
            return _section(
                "limit_up", "涨停", "failed", source="eastmoney_zt_pool",
                error=f"没有 data 对象（keys={list(payload)[:8]}）",
                note="未把失败写成涨停 0 家。",
            )
        pool = data.get("pool") or []
        total = data.get("tc")
        if total is None and not pool:
            return _section(
                "limit_up", "涨停", "failed", source="eastmoney_zt_pool",
                error=f"响应没有 tc 和 pool（data keys={list(data)[:8]}）",
                note="未把失败写成涨停 0 家。",
            )
        rows = []
        for item in pool:
            rows.append({
                "code": item.get("c"),
                "name": item.get("n"),
                "pct_change": astock_runtime.to_float(item.get("zdp")),
                "industry": item.get("hybk"),
                "p_raw": item.get("p"),
            })
        status = "partial" if total is not None and len(rows) < int(total) else "ok"
        return _section(
            "limit_up", "涨停", status, source="eastmoney_zt_pool", rows=rows,
            note=(
                "tc 是接口给出的涨停总数。表内只是 pagesize=5 的样本，不是完整名单。"
                "p_raw 是源字段原值，未换算成价格。"
            ),
            extra={"total": total, "qdate": data.get("qdate"), "sample_size": len(rows)},
        )
    return _guard("limit_up", "涨停", run)


def _quote_codes(transport, codes: list[str]):
    secids = ",".join(astock_runtime.em_secid(code) for code in codes)
    response = _call(
        transport,
        astock_runtime.EM_ULIST_URL,
        params={
            "fltt": "2",
            "invt": "2",
            "fields": "f12,f14,f2,f3,f6,f8,f9,f20,f21,f23",
            "secids": secids,
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        },
    )
    payload = response.json()
    diff = ((payload or {}).get("data") or {}).get("diff") or []
    if not isinstance(diff, list):
        raise RuntimeError("ulist diff 不是列表")
    return astock_runtime.normalize_ulist_rows(diff)


def _section_quote(transport, code: str):
    def run():
        rows = _quote_codes(transport, [code])
        matched = [row for row in rows if row["code"] == code]
        if not matched:
            return _section("quote", "公司概况", "failed", source="eastmoney_ulist",
                            error=f"{code} 不在返回结果中", note="未用全市场表查找，也未填 0 价格。")
        return _section("quote", "公司概况", "ok", source="eastmoney_ulist", rows=matched,
                        note="单只批量接口，total_mv 单位为亿元。")
    return _guard("quote", "公司概况", run)


def _section_technical(transport, code: str, days: int = 60):
    def run():
        response = _call(
            transport,
            astock_runtime.IFZQ_URL,
            params={"param": f"{astock_runtime.sina_symbol(code)},day,,,{int(days)},qfq"},
            headers={"User-Agent": "Mozilla/5.0"},
        )
        rows = astock_runtime.parse_ifzq_kline(response.json(), code)
        if len(rows) < 5:
            return _section("technical", "技术面", "failed", source="ifzq_gtimg",
                            error=f"K 线只有 {len(rows)} 根", note="样本不足，未计算均线。")
        closes = [row["close"] for row in rows if row.get("close") is not None]
        last = closes[-1]
        ma5 = sum(closes[-5:]) / 5 if len(closes) >= 5 else None
        ma20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else None
        trend = "样本不足，未判断趋势"
        if ma5 is not None and ma20 is not None:
            if last > ma5 > ma20:
                trend = "多头排列"
            elif last < ma5 < ma20:
                trend = "空头排列"
            else:
                trend = "震荡"
        return _section(
            "technical", "技术面", "ok" if ma20 is not None else "partial",
            source=rows[-1].get("source"),
            rows=[{
                "close": last,
                "ma5": ma5,
                "ma20": ma20,
                "trend": trend,
                "bars": len(rows),
                "adjust": rows[-1].get("adjust"),
                "last_date": rows[-1].get("date"),
            }],
            note="ifzq 前复权日 K，一次请求。均线只在样本够长时计算。",
        )
    return _guard("technical", "技术面", run)


def _section_flow(transport, code: str):
    def run():
        response = _call(
            transport,
            astock_runtime.EM_FFLOW_URL,
            params={
                "lmt": "5",
                "klt": "101",
                "secid": astock_runtime.em_secid(code),
                "fields1": "f1,f2,f3,f7",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63",
            },
        )
        data = (response.json() or {}).get("data") or {}
        klines = data.get("klines") or []
        if not klines:
            return _section("fund_flow", "资金面", "failed", source="eastmoney_fflow",
                            error="没有资金流 K 线")
        rows = []
        for line in klines:
            parts = str(line).split(",")
            if len(parts) < 2:
                continue
            rows.append({
                "date": parts[0],
                "main_net": astock_runtime.to_float(parts[1]),
                "raw_fields": len(parts),
            })
        return _section(
            "fund_flow", "资金面", "ok", source="eastmoney_fflow", rows=rows[-5:],
            note="主力净流入取 fields2 的 f52（第二列），单位为元。只请求最近 5 根。",
        )
    return _guard("fund_flow", "资金面", run)


def _section_news(transport, code: str):
    def run():
        param = {
            "uid": "",
            "keyword": code,
            "type": ["cmsArticleWebOld"],
            "client": "web",
            "clientType": "web",
            "clientVersion": "curr",
            "param": {
                "cmsArticleWebOld": {
                    "searchScope": "default",
                    "sort": "time",
                    "pageIndex": 1,
                    "pageSize": 5,
                    "preTag": "",
                    "postTag": "",
                }
            },
        }
        response = _call(
            transport,
            astock_runtime.EM_NEWS_URL,
            params={"cb": "cb", "param": json.dumps(param, ensure_ascii=False)},
        )
        payload = astock_runtime.parse_jsonp(response.text)
        articles = (((payload or {}).get("result") or {}).get("cmsArticleWebOld")) or []
        rows = []
        for article in articles[:5]:
            rows.append({
                "title": article.get("title"),
                "date": article.get("date"),
                "media": article.get("mediaName"),
                "url": article.get("url"),
            })
        if not rows:
            return _section("news", "舆情", "failed", source="eastmoney_keyword_search",
                            error="关键词搜索没有标题")
        return _section(
            "news", "舆情", "partial", source="eastmoney_keyword_search", rows=rows,
            note=(
                "这是代码关键词搜索命中，不是公司公告，标题可能只是在正文里提到代码。"
                f"hitsTotal={payload.get('hitsTotal')}。未核验正文。"
            ),
            extra={"hits_total": payload.get("hitsTotal")},
        )
    return _guard("news", "舆情", run)


def _base_report(kind, provenance, transport, date_note=None):
    return {
        "kind": kind,
        "provenance": provenance,
        "banner": astock_runtime.provenance_banner(provenance),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "date_note": date_note,
        "http_calls": list(getattr(transport, "calls", [])),
        "max_calls": getattr(transport, "max_calls", None),
        "timeout": getattr(transport, "timeout", None),
        "sections": [],
    }


def build_daily_report(date: Optional[str] = None, transport=None, provenance=None,
                       max_calls: int = 6, timeout: float = 8.0) -> dict:
    provenance, transport = _resolve_transport(transport, provenance, max_calls, timeout)
    today = datetime.now().strftime("%Y-%m-%d")
    report_date = date or today
    date_note = None
    if report_date != today:
        date_note = (
            "命令里的日期只写在标题上。指数、广度和板块是本次请求的最新快照，"
            "不是该历史交易日的重建。涨停接口会带上这个日期。"
        )
    report = _base_report("daily", provenance, transport, date_note)
    report["date"] = report_date
    report["sections"] = [
        _section_indices(transport),
        _section_breadth(transport),
        _section_industry(transport),
        _section_limit_up(transport, report_date if date else None),
    ]
    report["http_calls"] = list(getattr(transport, "calls", []))
    return report


def build_stock_report(code: str, transport=None, provenance=None,
                       max_calls: int = 5, timeout: float = 8.0) -> dict:
    code = astock_runtime.normalize_code(code)
    provenance, transport = _resolve_transport(transport, provenance, max_calls, timeout)
    report = _base_report("stock", provenance, transport)
    report["code"] = code
    report["sections"] = [
        _section_quote(transport, code),
        _section_technical(transport, code),
        _section_flow(transport, code),
        _section_news(transport, code),
    ]
    report["http_calls"] = list(getattr(transport, "calls", []))
    return report


def build_portfolio_report(config_path: Optional[str] = None, transport=None, provenance=None,
                           max_calls: int = 4, timeout: float = 8.0,
                           positions: Optional[list] = None, portfolio_name: str = "我的持仓") -> dict:
    provenance, transport = _resolve_transport(transport, provenance, max_calls, timeout)
    report = _base_report("portfolio", provenance, transport)
    if positions is None:
        loaded = _load_portfolio(config_path)
        if isinstance(loaded, str):
            report["sections"] = [_section("portfolio", "持仓", "failed", error=loaded)]
            return report
        positions = loaded.get("positions") or []
        portfolio_name = loaded.get("name") or portfolio_name
    report["name"] = portfolio_name
    if not positions:
        report["sections"] = [_section("portfolio", "持仓", "failed", error="持仓为空")]
        return report
    codes = []
    for position in positions:
        if position.get("code"):
            codes.append(astock_runtime.normalize_code(position["code"]))
    codes = list(dict.fromkeys(codes))
    if len(codes) > 30:
        report["sections"] = [_section(
            "portfolio", "持仓", "failed",
            error=f"持仓 {len(codes)} 只，超过单次 30 只上限。未分批扫全市场。",
        )]
        return report

    def run():
        quotes = _quote_codes(transport, codes)
        by_code = {row["code"]: row for row in quotes}
        rows = []
        missing = []
        for position in positions:
            if not position.get("code"):
                continue
            code = astock_runtime.normalize_code(position["code"])
            quote = by_code.get(code)
            if not quote or quote.get("price") is None:
                missing.append(code)
                rows.append({
                    "code": code,
                    "name": position.get("name"),
                    "status": "quote_missing",
                    "included_in_total": False,
                })
                continue
            cost = position.get("cost")
            shares = position.get("shares")
            price = quote["price"]
            row = {
                "code": code,
                "name": position.get("name") or quote.get("name"),
                "price": price,
                "pct_change_today": quote.get("pct_change"),
                "status": "ok",
                "included_in_total": cost is not None and shares is not None,
            }
            if row["included_in_total"]:
                cost = float(cost)
                shares = float(shares)
                market_value = price * shares
                cost_value = cost * shares
                row.update({
                    "cost": cost,
                    "shares": shares,
                    "market_value": market_value,
                    "cost_value": cost_value,
                    "profit": market_value - cost_value,
                    "profit_pct": ((price - cost) / cost * 100) if cost else None,
                })
            rows.append(row)
        return _section(
            "portfolio", "持仓", "partial" if missing else "ok",
            source="eastmoney_ulist", rows=rows,
            note="合计只包含同时具备成本、股数和现价的持仓。缺失行情不计入，也不按成本计价。",
            extra={"missing": missing},
        )
    report["sections"] = [_guard("portfolio", "持仓", run)]
    report["http_calls"] = list(getattr(transport, "calls", []))
    return report


def _load_portfolio(config_path: Optional[str]):
    candidates = [config_path] if config_path else [
        "./portfolio.yaml", "./portfolio.json", "~/.astock_skills/portfolio.yaml",
    ]
    path = None
    for candidate in candidates:
        if not candidate:
            continue
        expanded = os.path.expanduser(candidate)
        if os.path.exists(expanded):
            path = expanded
            break
    if not path:
        return "持仓配置文件不存在。未生成示例持仓。"
    try:
        if path.endswith((".yaml", ".yml")):
            import yaml
            with open(path, "r", encoding="utf-8") as handle:
                return yaml.safe_load(handle) or {}
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception as exc:
        return f"加载配置失败: {exc}"


def _render_section(section: dict) -> str:
    lines = [f"## {section['title']}", ""]
    lines.append(f"- 状态: **{section['status']}**")
    if section.get("source"):
        lines.append(f"- 来源: `{section['source']}`")
    if section.get("note"):
        lines.append(f"- 说明: {section['note']}")
    if section.get("error"):
        lines.append(f"- 错误: {section['error']}")
    if section.get("missing_symbols"):
        lines.append("- 缺失指数: " + ", ".join(section["missing_symbols"]))
    if section["id"] == "breadth" and section["status"] == "ok":
        lines.append(
            f"- 上涨桶: **{section.get('up')}**  下跌桶: **{section.get('down')}**  "
            f"平盘桶: **{section.get('flat')}**  合计: {section.get('counted')}"
        )
        lines.append(f"- 分布日期: {section.get('qdate')}")
    if section["id"] == "limit_up":
        lines.append(f"- 接口涨停总数 tc: {section.get('total')}")
        lines.append(f"- 分布日期: {section.get('qdate')}")
    rows = section.get("rows") or []
    if section["status"] in {"ok", "partial"} and rows and section["id"] == "indices":
        lines.extend(["", "| 名称 | 代码 | 点位 | 涨跌幅 |", "|------|------|------|--------|"])
        for row in rows:
            lines.append(
                f"| {row.get('name') or '-'} | {row.get('code') or '-'} | "
                f"{fmt_num(row.get('price'))} | {fmt_pct(row.get('pct_change'))} |"
            )
    elif section["id"] == "industry" and rows:
        lines.extend(["", "| 行业 | 涨跌幅 | 领涨股 |", "|------|--------|--------|"])
        for row in rows:
            lines.append(
                f"| {row.get('name') or '-'} | {fmt_pct(row.get('pct_change'))} | {row.get('leader') or '-'} |"
            )
    elif section["id"] == "limit_up" and rows:
        lines.extend(["", "| 代码 | 名称 | 涨跌幅 | 行业 | p_raw |", "|------|------|--------|------|-------|"])
        for row in rows:
            lines.append(
                f"| {row.get('code') or '-'} | {row.get('name') or '-'} | "
                f"{fmt_pct(row.get('pct_change'))} | {row.get('industry') or '-'} | {row.get('p_raw')} |"
            )
    elif section["id"] == "quote" and rows:
        row = rows[0]
        lines.extend([
            "",
            f"- 名称: {row.get('name') or '-'}",
            f"- 现价: {fmt_num(row.get('price'))}",
            f"- 涨跌幅: {fmt_pct(row.get('pct_change'))}",
            f"- 总市值: {fmt_num(row.get('total_mv'))} 亿元",
            f"- PE: {fmt_num(row.get('pe'))}",
            f"- PB: {fmt_num(row.get('pb'))}",
        ])
    elif section["id"] == "technical" and rows:
        row = rows[0]
        lines.extend([
            "",
            f"- 日期: {row.get('last_date')}",
            f"- 收盘: {fmt_num(row.get('close'))}",
            f"- MA5: {fmt_num(row.get('ma5'))}",
            f"- MA20: {fmt_num(row.get('ma20'))}",
            f"- 趋势: {row.get('trend')}",
            f"- K 线根数: {row.get('bars')}  复权: {row.get('adjust')}",
        ])
    elif section["id"] == "fund_flow" and rows:
        lines.extend(["", "| 日期 | 主力净流入 |", "|------|------------|"])
        for row in rows:
            lines.append(f"| {row.get('date')} | {fmt_money(row.get('main_net'))} |")
    elif section["id"] == "news" and rows:
        lines.append("")
        for row in rows:
            title = row.get("title") or "-"
            lines.append(f"- {row.get('date') or '-'} {title}")
    elif section["id"] == "portfolio" and rows:
        lines.extend(["", "| 代码 | 名称 | 状态 | 现价 | 盈亏 | 收益率 |", "|------|------|------|------|------|--------|"])
        total_mv = total_cost = 0.0
        counted = 0
        for row in rows:
            profit = fmt_money(row["profit"]) if row.get("included_in_total") else "-"
            profit_pct = fmt_pct(row.get("profit_pct")) if row.get("included_in_total") else "-"
            lines.append(
                f"| {row.get('code')} | {row.get('name') or '-'} | {row.get('status')} | "
                f"{fmt_num(row.get('price'))} | {profit} | {profit_pct} |"
            )
            if row.get("included_in_total"):
                total_mv += row["market_value"]
                total_cost += row["cost_value"]
                counted += 1
        lines.append("")
        lines.append(f"- 计入合计的持仓: {counted} 只")
        lines.append(f"- 这部分市值: {total_mv:,.2f} 元")
        lines.append(f"- 这部分成本: {total_cost:,.2f} 元")
        if counted:
            lines.append(f"- 这部分盈亏: {total_mv - total_cost:+,.2f} 元")
        if section.get("missing"):
            lines.append("- 行情缺失未计入: " + ", ".join(section["missing"]))
    lines.append("")
    return "\n".join(lines)


def render_markdown(report: dict) -> str:
    title = {
        "daily": "A股每日复盘报告",
        "stock": f"个股研究报告: {report.get('code', '')}",
        "portfolio": f"持仓报告 - {report.get('name', '')}",
    }.get(report.get("kind"), "报告")
    lines = [
        f"# {title}",
        "",
        f"**数据属性**: {report.get('banner')}",
        f"**生成时间**: {report.get('generated_at')}",
    ]
    if report.get("date"):
        lines.append(f"**标题日期**: {report['date']}")
    if report.get("date_note"):
        lines.append(f"**日期说明**: {report['date_note']}")
    calls = report.get("http_calls") or []
    lines.append(f"**HTTP 调用**: {sum(1 for item in calls if item.get('ok'))} 成功 / {len(calls)} 次记录")
    if report.get("max_calls") is not None:
        lines.append(f"**预算**: 最多 {report['max_calls']} 次，单次超时 {report.get('timeout')} 秒")
    lines.extend(["", "---", ""])
    for section in report.get("sections") or []:
        lines.append(_render_section(section))
    lines.append("---")
    lines.append("")
    lines.append("本报告只整理公开行情，不构成投资建议。失败的小节没有用估算或合成数据补齐。")
    lines.append("")
    return "\n".join(lines)


def report_daily(date: Optional[str] = None, save: bool = False, **kwargs) -> str:
    report = build_daily_report(date=date, **kwargs)
    text = render_markdown(report)
    if save:
        path = f"daily_report_{report['date']}.md"
        Path(path).write_text(text, encoding="utf-8")
        text += f"\n报告已保存: `{path}`\n"
    return text


def report_stock(code: str, save: bool = False, **kwargs) -> str:
    report = build_stock_report(code, **kwargs)
    text = render_markdown(report)
    if save:
        path = f"research_{report['code']}_{datetime.now().strftime('%Y%m%d')}.md"
        Path(path).write_text(text, encoding="utf-8")
        text += f"\n报告已保存: `{path}`\n"
    return text


def report_portfolio(config_path: Optional[str] = None, save: bool = False, **kwargs) -> str:
    report = build_portfolio_report(config_path=config_path, **kwargs)
    text = render_markdown(report)
    if save and report["sections"] and report["sections"][0]["status"] != "failed":
        path = f"portfolio_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
        Path(path).write_text(text, encoding="utf-8")
        text += f"\n报告已保存: `{path}`\n"
    return text


def _exit_code(report: dict) -> int:
    statuses = [section["status"] for section in report.get("sections") or []]
    if any(status in {"ok", "partial"} for status in statuses):
        return 0
    return 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="report（有界公开行情，失败不编造）")
    sub = parser.add_subparsers(dest="cmd")

    daily = sub.add_parser("daily", help="每日复盘")
    daily.add_argument("--date")
    daily.add_argument("--save", action="store_true")
    daily.add_argument("--json", action="store_true")
    daily.add_argument("--max-calls", type=int, default=6)
    daily.add_argument("--timeout", type=float, default=8.0)

    stock = sub.add_parser("stock", help="个股研报")
    stock.add_argument("code")
    stock.add_argument("--save", action="store_true")
    stock.add_argument("--json", action="store_true")
    stock.add_argument("--max-calls", type=int, default=5)
    stock.add_argument("--timeout", type=float, default=8.0)

    portfolio = sub.add_parser("portfolio", help="持仓报告")
    portfolio.add_argument("--config")
    portfolio.add_argument("--save", action="store_true")
    portfolio.add_argument("--json", action="store_true")
    portfolio.add_argument("--max-calls", type=int, default=4)
    portfolio.add_argument("--timeout", type=float, default=8.0)
    args = parser.parse_args(argv)

    if args.cmd == "daily":
        report = build_daily_report(date=args.date, max_calls=args.max_calls, timeout=args.timeout)
        text = render_markdown(report)
        if args.save:
            path = f"daily_report_{report['date']}.md"
            Path(path).write_text(text, encoding="utf-8")
            text += f"\n报告已保存: `{path}`\n"
    elif args.cmd == "stock":
        report = build_stock_report(args.code, max_calls=args.max_calls, timeout=args.timeout)
        text = render_markdown(report)
        if args.save:
            path = f"research_{report['code']}_{datetime.now().strftime('%Y%m%d')}.md"
            Path(path).write_text(text, encoding="utf-8")
            text += f"\n报告已保存: `{path}`\n"
    elif args.cmd == "portfolio":
        report = build_portfolio_report(config_path=args.config, max_calls=args.max_calls, timeout=args.timeout)
        text = render_markdown(report)
        if args.save and report["sections"] and report["sections"][0]["status"] != "failed":
            path = f"portfolio_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
            Path(path).write_text(text, encoding="utf-8")
            text += f"\n报告已保存: `{path}`\n"
    else:
        parser.print_help()
        return 0

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        print(text)
    return _exit_code(report)


if __name__ == "__main__":
    sys.exit(main())
