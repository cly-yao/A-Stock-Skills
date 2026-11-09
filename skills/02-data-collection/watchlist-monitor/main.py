#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""watchlist-monitor: 自选股实时监控"""

import os
import sys
import json
import time
import argparse
import pandas as pd
from pathlib import Path
from typing import List, Dict, Optional
from datetime import datetime

for _candidate in Path(__file__).resolve().parents:
    if (_candidate / "astock_runtime.py").is_file() and (_candidate / "skills").is_dir():
        if str(_candidate) not in sys.path:
            sys.path.insert(0, str(_candidate))
        break

import astock_runtime


# ============================================================
# 配置加载
# ============================================================
DEFAULT_CONFIG_PATHS = [
    "./watchlist.yaml",
    "./watchlist.yml",
    "./watchlist.json",
    "~/.astock_skills/watchlist.yaml",
    "~/.astock_skills/watchlist.json",
]


def load_yaml(path: str) -> Dict:
    try:
        import yaml
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except ImportError:
        print("需要安装 pyyaml: pip install pyyaml", file=sys.stderr)
        return {}


def load_json(path: str) -> Dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"加载 JSON 失败: {e}", file=sys.stderr)
        return {}


def load_watchlist(path: Optional[str] = None) -> Dict:
    """加载自选股配置"""
    if path:
        if not os.path.exists(path):
            print(f"配置文件不存在: {path}", file=sys.stderr)
            return {"stocks": [], "alerts": {}}
        if path.endswith((".yaml", ".yml")):
            return load_yaml(path)
        else:
            return load_json(path)

    # 自动查找默认配置
    for p in DEFAULT_CONFIG_PATHS:
        p = os.path.expanduser(p)
        if os.path.exists(p):
            print(f"使用配置: {p}", file=sys.stderr)
            if p.endswith((".yaml", ".yml")):
                return load_yaml(p)
            return load_json(p)

    print("未找到配置文件, 使用空配置", file=sys.stderr)
    return {"stocks": [], "alerts": {}}


def save_watchlist_template(path: str = "./watchlist.yaml"):
    """生成配置模板"""
    template = """# A股自选股监控配置
# 用法: python main.py monitor --config watchlist.yaml

# ============== 自选股列表 ==============
stocks:
  - code: "000001"
    name: "平安银行"
    cost: 12.50        # 持仓成本 (可选, 用于计算盈亏)
    shares: 1000       # 持仓数量 (可选)
    note: "银行龙头"   # 备注 (可选)
  - code: "600519"
    name: "贵州茅台"
  - code: "300750"
    name: "宁德时代"
  - code: "000858"
    name: "五粮液"

# ============== 告警阈值 ==============
alerts:
  # 涨跌幅告警 (%)
  pct_change_up: 5.0      # 涨幅超过此值告警
  pct_change_down: -3.0   # 跌幅超过此值告警

  # 价格告警 (元)
  price_above: 100        # 价格突破此值告警
  price_below: 50         # 价格跌破此值告警

  # 量能告警
  volume_ratio: 3.0       # 量比超过此值告警

  # 涨跌停告警
  limit_up: true          # 涨停提醒
  limit_down: true        # 跌停提醒

# ============== 数据源 (高级) ==============
# 留空则使用 akshare 本地
# source:
#   type: "ths"            # 同花顺
#   cookie: "your_cookie"  # 需要登录后从浏览器获取
#   # 或
#   type: "eastmoney"      # 东方财富
#   cookie: "your_cookie"
"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(template)
    print(f"配置模板已生成: {path}")


# ============================================================
# 数据获取
# ============================================================
def _missing_quote(code: str, error: str) -> Dict:
    return {
        "code": code,
        "name": "",
        "price": None,
        "pct_change": None,
        "found": False,
        "error": error,
    }


def _fetch_quote_frame(codes: List[str]):
    """One Eastmoney batch. Tests can replace this; it must not page the whole market."""
    module = astock_runtime.load_py(
        Path(astock_runtime.repo_root_from(Path(__file__))) / "skills/01-infra/astock-data-source/main.py",
        "watchlist_data_source",
    )
    return module.get_quotes_for_codes(codes, timeout=8)


def get_realtime_quotes(codes: List[str], source_cookie: Optional[str] = None) -> List[Dict]:
    """批量获取实时行情。最多 50 只，一次请求。找不到的代码不填 0。"""
    normalized = []
    for code in codes:
        try:
            normalized.append(astock_runtime.normalize_code(code))
        except ValueError:
            normalized.append(str(code))
    if source_cookie:
        print("cookie 行情源未实现，未改用全市场接口。", file=sys.stderr)
        return [_missing_quote(code, "cookie source not implemented") for code in normalized]
    if len(normalized) > 50:
        print("超过 50 只，拒绝拉取。未改用全市场分页接口。", file=sys.stderr)
        return [_missing_quote(code, "code list exceeds 50") for code in normalized]
    try:
        frame = _fetch_quote_frame(normalized)
    except Exception as exc:
        print(f"获取行情失败: {exc}", file=sys.stderr)
        return [_missing_quote(code, f"{type(exc).__name__}: {exc}") for code in normalized]
    by_code = {}
    if frame is not None and not frame.empty:
        by_code = {str(row["code"]).zfill(6): row for _, row in frame.iterrows()}
    quotes = []
    for code in normalized:
        row = by_code.get(code)
        if row is None or row.get("price") is None or pd.isna(row.get("price")):
            quotes.append(_missing_quote(code, "quote missing"))
            continue
        total_yi = row.get("total_mv")
        circ_yi = row.get("circ_mv")
        quotes.append({
            "code": code,
            "name": row.get("name") or "",
            "price": float(row["price"]),
            "pct_change": None if row.get("pct_change") is None or pd.isna(row.get("pct_change")) else float(row["pct_change"]),
            "amount": None if row.get("amount") is None or pd.isna(row.get("amount")) else float(row["amount"]),
            "turnover": None if row.get("turnover") is None or pd.isna(row.get("turnover")) else float(row["turnover"]),
            "pe": None if row.get("pe") is None or pd.isna(row.get("pe")) else float(row["pe"]),
            "pb": None if row.get("pb") is None or pd.isna(row.get("pb")) else float(row["pb"]),
            # 旧字段 total_mv 按元保留；亿元写在 total_mv_yi，避免把单位换了却不说明。
            "total_mv": None if total_yi is None or pd.isna(total_yi) else float(total_yi) * 1e8,
            "total_mv_yi": None if total_yi is None or pd.isna(total_yi) else float(total_yi),
            "circ_mv": None if circ_yi is None or pd.isna(circ_yi) else float(circ_yi) * 1e8,
            "total_mv_unit": "total_mv 为元，total_mv_yi 为亿元",
            "source": row.get("source"),
            "found": True,
        })
    return quotes


def get_volume_ratio(code: str):
    """当日成交量 / 此前 5 根均量。失败时返回 None，不返回 1。"""
    try:
        import requests
        response = requests.get(
            astock_runtime.IFZQ_URL,
            params={"param": f"{astock_runtime.sina_symbol(code)},day,,,10,qfq"},
            timeout=8,
            proxies={"http": None, "https": None},
            headers={"User-Agent": "Mozilla/5.0"},
        )
        response.raise_for_status()
        rows = astock_runtime.parse_ifzq_kline(response.json(), code)
        volumes = [row["volume"] for row in rows if row.get("volume") is not None]
        if len(volumes) < 6 or volumes[-6:-1] and sum(volumes[-6:-1]) == 0:
            return None
        base = sum(volumes[-6:-1]) / 5
        if base == 0:
            return None
        return round(volumes[-1] / base, 2)
    except Exception:
        return None


# ============================================================
# 告警检查
# ============================================================
def check_alerts(quotes: List[Dict], alerts: Dict, stock_configs: List[Dict]) -> List[Dict]:
    """检查告警"""
    triggered = []
    cost_map = {s["code"]: s for s in stock_configs if "code" in s}

    for q in quotes:
        if not q.get("found"):
            continue
        code = q["code"]
        price = q.get("price")
        pct = q.get("pct_change")

        # 涨幅告警
        if pct is not None and "pct_change_up" in alerts and pct >= alerts["pct_change_up"]:
            triggered.append({
                "code": code, "name": q["name"], "type": "pct_change_up",
                "value": pct, "message": f"涨幅 {pct:+.2f}%, 超过阈值 {alerts['pct_change_up']}%"
            })
        # 跌幅告警
        if pct is not None and "pct_change_down" in alerts and pct <= alerts["pct_change_down"]:
            triggered.append({
                "code": code, "name": q["name"], "type": "pct_change_down",
                "value": pct, "message": f"跌幅 {pct:+.2f}%, 超过阈值 {alerts['pct_change_down']}%"
            })
        # 价格上限
        if price is not None and "price_above" in alerts and price >= alerts["price_above"]:
            triggered.append({
                "code": code, "name": q["name"], "type": "price_above",
                "value": price, "message": f"价格 {price}, 突破 {alerts['price_above']}"
            })
        # 价格下限
        if price is not None and "price_below" in alerts and price <= alerts["price_below"]:
            triggered.append({
                "code": code, "name": q["name"], "type": "price_below",
                "value": price, "message": f"价格 {price}, 跌破 {alerts['price_below']}"
            })
        # 涨停
        if pct is not None and alerts.get("limit_up") and pct >= 9.9:
            triggered.append({
                "code": code, "name": q["name"], "type": "limit_up",
                "value": pct, "message": f"涨停! +{pct:.2f}%"
            })
        # 跌停
        if pct is not None and alerts.get("limit_down") and pct <= -9.9:
            triggered.append({
                "code": code, "name": q["name"], "type": "limit_down",
                "value": pct, "message": f"跌停! {pct:.2f}%"
            })

    return triggered


# ============================================================
# 显示
# ============================================================
def print_quotes(quotes: List[Dict], stock_configs: List[Dict]):
    """打印行情"""
    cost_map = {s["code"]: s for s in stock_configs if "code" in s}
    print(f"\n📊 自选股监控 ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')})")
    print("=" * 70)
    for q in quotes:
        if not q.get("found"):
            print(f"❌ {q['code']:<8} 未找到")
            continue
        pct = q.get("pct_change")
        icon = "➖" if pct is None else "🔺" if pct > 0 else "🔻" if pct < 0 else "➖"
        pct_text = "   -  " if pct is None else f"{pct:+6.2f}%"
        cost_str = ""
        cfg = cost_map.get(q["code"])
        if cfg and cfg.get("cost") not in (None, 0) and q.get("price") is not None:
            profit = (q["price"] - cfg["cost"]) * cfg.get("shares", 0)
            profit_pct = (q["price"] - cfg["cost"]) / cfg["cost"] * 100
            cost_str = f"  盈亏: {profit:+,.0f} ({profit_pct:+.2f}%)"
        print(f"{icon} {q['code']:<6} {q['name']:<8} {q['price']:>8.2f}  {pct_text}{cost_str}")
    print("=" * 70)


def print_alerts(alerts: List[Dict]):
    """打印告警"""
    if not alerts:
        print("\n✅ 无告警")
        return
    print(f"\n🚨 触发告警 {len(alerts)} 条:")
    for a in alerts:
        print(f"  [{a['type']}] {a['code']} {a['name']}: {a['message']}")


# ============================================================
# 监控主循环
# ============================================================
def monitor_once(config_path: Optional[str] = None) -> Dict:
    """单次监控"""
    config = load_watchlist(config_path)
    stocks = config.get("stocks", [])
    alert_cfg = config.get("alerts", {})
    source = config.get("source", {})

    if not stocks:
        print("自选股为空, 请先配置 watchlist.yaml", file=sys.stderr)
        return {}

    codes = [s["code"] for s in stocks]
    quotes = get_realtime_quotes(codes, source.get("cookie"))
    print_quotes(quotes, stocks)

    alerts = check_alerts(quotes, alert_cfg, stocks)
    print_alerts(alerts)

    return {"quotes": quotes, "alerts": alerts}


def monitor_loop(config_path: Optional[str] = None, interval: int = 30):
    """持续监控"""
    print(f"⏰ 持续监控, 间隔 {interval} 秒, Ctrl+C 停止")
    try:
        while True:
            monitor_once(config_path)
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n已停止监控")


# ============================================================
# CLI
# ============================================================
def main():
    parser = argparse.ArgumentParser(description="watchlist-monitor")
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("monitor", help="单次监控")
    p.add_argument("--config", help="配置文件路径")
    p.add_argument("--codes", help="股票代码, 逗号分隔")

    p = sub.add_parser("loop", help="持续监控")
    p.add_argument("--config")
    p.add_argument("--interval", type=int, default=30)

    p = sub.add_parser("init", help="生成配置模板")
    p.add_argument("--path", default="./watchlist.yaml")

    p = sub.add_parser("alerts", help="仅检查告警")
    p.add_argument("--config")

    args = parser.parse_args()

    if args.cmd == "init":
        save_watchlist_template(args.path)
    elif args.cmd == "monitor":
        if args.codes:
            codes = [c.strip() for c in args.codes.split(",")]
            quotes = get_realtime_quotes(codes)
            print_quotes(quotes, [{"code": c} for c in codes])
        else:
            monitor_once(args.config)
    elif args.cmd == "loop":
        monitor_loop(args.config, args.interval)
    elif args.cmd == "alerts":
        r = monitor_once(args.config)
        # monitor_once 已打印告警
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
