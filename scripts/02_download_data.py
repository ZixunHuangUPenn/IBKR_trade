"""
第 2 课：把历史数据落到本地。

    python scripts/02_download_data.py                          # 默认标的池，5年日线
    python scripts/02_download_data.py --symbols SPY,QQQ,TLT
    python scripts/02_download_data.py --duration "10 Y" --bar-size "1 day"
    python scripts/02_download_data.py --what-to-show TRADES    # 不复权

为什么要落地，而不是每次现拉：
  - IBKR 有限速（10 分钟约 60 次请求），反复拉很快就被拒
  - 回测要可复现。数据存下来，你才能确定这次和上次跑的是同一批
  - 离线也能改策略、跑回测，不用开着 Gateway

复权（ADJUSTED_LAST）vs 不复权（TRADES）：
  分红除权日，未复权价格会有个"假跌幅"。用它回测分红率高的品种
  （TLT、VNQ、SPY 都不低），长期收益会被系统性低估几个百分点。
  默认用复权价。
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
from ibkr.connection import IBConnection
from ibkr.market_data import download_universe

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def main() -> int:
    ap = argparse.ArgumentParser(description="下载 IBKR 历史K线到本地缓存")
    ap.add_argument("--symbols", default=",".join(config.DEFAULT_UNIVERSE),
                    help="逗号分隔，默认是 config.DEFAULT_UNIVERSE")
    ap.add_argument("--duration", default="5 Y", help='如 "5 Y" / "6 M" / "90 D"')
    ap.add_argument("--bar-size", default="1 day", help='如 "1 day" / "1 hour" / "5 mins"')
    ap.add_argument("--what-to-show", default="ADJUSTED_LAST",
                    choices=["ADJUSTED_LAST", "TRADES", "MIDPOINT"])
    ap.add_argument("--include-premarket", action="store_true",
                    help="包含盘前盘后（默认只要正常交易时段）")
    args = ap.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    est_sec = len(symbols) * 2.5
    print(f"准备下载 {len(symbols)} 个标的: {', '.join(symbols)}")
    print(f"周期={args.bar_size}  区间={args.duration}  类型={args.what_to_show}")
    print(f"因为要遵守 IBKR 限速，预计耗时 ≥{est_sec:.0f} 秒\n")

    with IBConnection() as ib:
        data = download_universe(
            ib, symbols,
            duration=args.duration,
            bar_size=args.bar_size,
            what_to_show=args.what_to_show,
            use_rth=not args.include_premarket,
        )

    print(f"\n成功 {len(data)}/{len(symbols)} 个，缓存在 {config.BARS_DIR}")
    for s, df in data.items():
        print(f"  {s:6} {len(df):>6} 根  {df.index[0].date()} ~ {df.index[-1].date()}")

    if data:
        print("\n下一步: python scripts/03_run_backtest.py")
    return 0 if len(data) == len(symbols) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as e:
        print(f"\n[失败] {e}", file=sys.stderr)
        raise SystemExit(1)
