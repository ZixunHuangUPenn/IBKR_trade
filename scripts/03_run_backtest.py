"""
第 3 课：用真实数据跑回测。不需要连 IBKR，读本地缓存就行。

    python scripts/03_run_backtest.py                       # 跑全部策略做对比
    python scripts/03_run_backtest.py --strategy 横截面动量 --params top_n=3
    python scripts/03_run_backtest.py --symbols SPY,TLT,GLD --band 0.05

看结果的顺序：
  1. 先看它有没有打赢"等权买入持有"这条基准。打不赢就没必要继续。
  2. 再看最大回撤。年化高但回撤 50% 的策略，你实盘拿不住。
  3. 再看年换手。几百个百分点以上，就要认真核算成本假设是否乐观了。
  4. 最后做参数敏感性检查：微调参数后结果剧变 = 过拟合。
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

import config
from backtest.engine import buy_and_hold
from backtest.metrics import compare
from ibkr.market_data import cached_symbols, load_universe_prices
from scripts_util import parse_params
from strategies import REGISTRY, build

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


def main() -> int:
    ap = argparse.ArgumentParser(description="用本地缓存数据跑回测")
    ap.add_argument("--symbols", default="", help="逗号分隔，留空 = 所有已缓存的标的")
    ap.add_argument("--strategy", default="", help=f"留空 = 全部。可选: {list(REGISTRY)}")
    ap.add_argument("--params", nargs="*", default=[], help="如 top_n=3 lookback=6")
    ap.add_argument("--band", type=float, default=0.0, help="不调仓缓冲区")
    ap.add_argument("--commission", type=float, default=config.COMMISSION_BPS)
    ap.add_argument("--slippage", type=float, default=config.SLIPPAGE_BPS)
    ap.add_argument("--lag", type=int, default=1, help="信号延迟；0 = 故意的未来函数")
    args = ap.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if not symbols:
        symbols = cached_symbols()
        if not symbols:
            print("本地没有任何缓存数据。先跑 scripts/02_download_data.py，"
                  "或先用 scripts/00_offline_demo.py 熟悉流程。", file=sys.stderr)
            return 1

    prices = load_universe_prices(symbols)
    print(f"标的池 ({prices.shape[1]}): {', '.join(prices.columns)}")
    print(f"区间: {prices.index[0].date()} ~ {prices.index[-1].date()} "
          f"({len(prices)} 个交易日)\n")

    kw = dict(commission_bps=args.commission, slippage_bps=args.slippage, lag=args.lag)

    results = [buy_and_hold(prices, **kw)]
    names = [args.strategy] if args.strategy else list(REGISTRY)
    extra = parse_params(args.params)

    for name in names:
        if name not in REGISTRY:
            print(f"没有策略 {name!r}，可选: {list(REGISTRY)}", file=sys.stderr)
            return 1
        try:
            strat = build(name, **(extra if args.strategy else {}))
            results.append(strat.backtest(prices, rebalance_band=args.band, **kw))
        except Exception as e:  # noqa: BLE001
            print(f"{name} 回测失败: {e}", file=sys.stderr)

    print("=" * 104)
    for r in results:
        print(r.summary())
    print("=" * 104)

    table = compare({r.name: r.metrics for r in results})
    print("\n" + table.to_string(float_format=lambda x: f"{x:.3f}"))

    eq = pd.DataFrame({r.name: r.equity for r in results})
    out = config.RESULTS_DIR / "backtest_equity.csv"
    eq.to_csv(out, encoding="utf-8-sig")
    table.to_csv(config.RESULTS_DIR / "backtest_metrics.csv", encoding="utf-8-sig")
    print(f"\n结果已存到 {config.RESULTS_DIR}")
    print("下一步: streamlit run dashboard/app.py   （交互式调参更快）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
