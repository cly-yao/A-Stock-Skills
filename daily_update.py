#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""daily_update.py: 更新少量 K 线缓存。

用法:
    python daily_update.py --codes 000001,600519
    python daily_update.py --max-codes 5 --days 60

不带 --codes 且不带 --max-codes 时直接退出，避免全市场逐只下载。

建议 cron (Linux/Mac):
    30 15 * * 1-5 cd /path/to/A-Stock-Skills && python daily_update.py --max-codes 20

建议 Windows 任务计划程序:
    触发器: 周一-周五 15:30
    操作: python D:\myCode\python\a_stock_skills\daily_update.py --max-codes 20
"""

import sys
import time
import argparse
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import astock_runtime

_ds = astock_runtime.load_py(ROOT / "skills/01-infra/astock-data-source/main.py", "daily_update_ds")
_cache = astock_runtime.load_py(ROOT / "skills/01-infra/astock-cache/main.py", "daily_update_cache")
get_realtime_all = _ds.get_realtime_all
get_kline = _ds.get_kline
kline_save = _cache.kline_save
kline_stats = _cache.kline_stats


def get_all_codes(limit: int):
    """拿一页行情里的代码。limit 是请求条数，不是全市场认定。"""
    print(f"拉取一页行情代码，最多 {limit} 只（不是全市场普查）...")
    df = get_realtime_all(num=limit, timeout=12)
    if df is None or df.empty:
        print("拉取失败。未改用其他清单填充。")
        return []
    if "code" not in df.columns:
        print("返回里没有 code 列。")
        return []
    codes = df["code"].astype(str).str.zfill(6).tolist()
    print(
        f"拿到 {len(codes)} 只；universe_complete={df.attrs.get('universe_complete')}"
    )
    return codes


def update_one(code, days=60):
    """更新单只 K 线"""
    try:
        df = get_kline(code, days=days)
        if df is not None and not df.empty:
            kline_save(code, df, days=days)
            return True
    except Exception as e:
        print(f"  ⚠️ {code}: {e}", file=sys.stderr)
    return False


def main():
    parser = argparse.ArgumentParser(description="每日 K 线缓存更新")
    parser.add_argument("--codes", help="指定股票代码,逗号分隔")
    parser.add_argument("--days", type=int, default=60, help="K 线天数 (默认 60)")
    parser.add_argument("--workers", type=int, default=1, help="保留参数。实际仍逐只更新")
    parser.add_argument("--max-codes", type=int, default=None,
                        help="未指定 --codes 时，从一页行情最多取多少只")
    args = parser.parse_args()

    print("=" * 60)
    print(f"📊 每日 K 线更新 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"   K 线天数: {args.days}")
    print("=" * 60)

    if args.codes:
        codes = [c.strip().zfill(6) for c in args.codes.split(",") if c.strip()]
        if args.max_codes is not None:
            codes = codes[:args.max_codes]
    elif args.max_codes is None:
        print("UNBOUNDED_REFUSED: 请传 --codes 或 --max-codes。不会默认下载全市场 K 线。")
        return 2
    elif args.max_codes < 1 or args.max_codes > 50:
        print("--max-codes 必须在 1 到 50 之间。")
        return 2
    else:
        codes = get_all_codes(args.max_codes)

    if not codes:
        print("没有股票代码, 退出")
        return 1

    print(f"🔄 开始更新 {len(codes)} 只股票 K 线...")
    start = time.time()
    success = 0
    fail = 0

    for i, code in enumerate(codes, 1):
        if update_one(code, args.days):
            success += 1
        else:
            fail += 1

        # 每 100 只打印进度
        if i % 100 == 0:
            elapsed = time.time() - start
            speed = i / elapsed
            eta = (len(codes) - i) / speed
            print(f"  [{i}/{len(codes)}] 成功 {success} 失败 {fail} "
                  f"速度 {speed:.1f} 只/秒 剩余 {eta/60:.1f} 分钟")

    elapsed = time.time() - start
    print("=" * 60)
    print(f"✅ 完成! 成功 {success} / 失败 {fail}  耗时 {elapsed/60:.1f} 分钟")
    print(f"缓存统计: {kline_stats()}")
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
