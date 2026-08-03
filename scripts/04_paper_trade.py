"""
第 4 课：把策略信号变成 Paper 账户里的真实订单。

    python scripts/04_paper_trade.py --strategy 横截面动量            # 只看不下（默认）
    python scripts/04_paper_trade.py --strategy 横截面动量 --what-if  # 让 IBKR 预演保证金
    python scripts/04_paper_trade.py --strategy 横截面动量 --execute  # 真下（Paper）

流程：
    读本地价格 -> 策略算出目标权重 -> 读账户净值和当前持仓
    -> 取实时价 -> 算出差额订单 -> 体检 -> 下单 -> 等成交 -> 存快照

这一步最该关注的不是"能不能下单"，而是回测和实盘之间的三道缝：

  1. 价格不一样  回测用的是收盘价，实盘成交在你下单那一刻的价格。
  2. 数量是整数  回测里权重是连续的，实盘只能买整股，小账户误差尤其大。
  3. 时点不一样  回测假设收盘价成交，你实际是在盘中某个时刻下的单。

先在 Paper 上跑够久，把这三道缝的影响量化出来，再谈实盘。
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
from ibkr import account as acct
from ibkr.connection import IBConnection
from ibkr.execution import execute_plan, weights_to_orders
from ibkr.market_data import cached_symbols, load_universe_prices, make_contract
from scripts_util import parse_params
from strategies import REGISTRY, build

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("paper_trade")


def live_prices(ib, symbols: list[str], fallback: dict[str, float]) -> dict[str, float]:
    """
    取当前价。拿不到就退回最近的收盘价 —— 但要吭声，
    因为用隔夜的价格算数量，跳空大的时候会算错不少。
    """
    contracts = [make_contract(s) for s in symbols]
    ib.qualifyContracts(*contracts)
    tickers = ib.reqTickers(*contracts)

    out = {}
    for t in tickers:
        px = t.marketPrice()
        sym = t.contract.symbol
        if px and px == px and px > 0:
            out[sym] = px
        else:
            out[sym] = fallback[sym]
            log.warning("%s 取不到实时价（可能是收盘或没有行情权限），"
                        "退回缓存收盘价 %.2f", sym, fallback[sym])
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="按策略信号在 Paper 账户下单")
    ap.add_argument("--strategy", required=True, help=f"可选: {list(REGISTRY)}")
    ap.add_argument("--params", nargs="*", default=[])
    ap.add_argument("--symbols", default="", help="留空 = config.DEFAULT_UNIVERSE")
    ap.add_argument("--execute", action="store_true", help="真正发送订单")
    ap.add_argument("--what-if", action="store_true",
                    help="发给 IBKR 做保证金预演，不成交")
    ap.add_argument("--order-type", default="LMT", choices=["LMT", "MKT"])
    ap.add_argument("--band", type=float, default=0.01, help="权重偏离阈值")
    ap.add_argument("--min-trade", type=float, default=200.0, help="最小交易金额")
    args = ap.parse_args()

    if args.execute and config.is_live_port() and not config.ALLOW_LIVE:
        print("拒绝：--execute + 实盘端口，但 IB_ALLOW_LIVE=false。", file=sys.stderr)
        return 1

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    # 默认用 DEFAULT_UNIVERSE，不是 cached_symbols()。
    # 缓存里还躺着 00_offline_demo.py 写的 SYNTH_* 合成数据 —— 把它们喂给横截面动量，
    # 排名会被合成序列占满，真 ETF 全被挤出去，而 SYNTH_* 根本不是可交易的合约。
    symbols = symbols or list(config.DEFAULT_UNIVERSE)
    missing_cache = [s for s in symbols if s not in cached_symbols()]
    if missing_cache:
        print(f"这些标的没有本地缓存: {missing_cache}\n"
              f"先跑 scripts/02_download_data.py", file=sys.stderr)
        return 1

    prices = load_universe_prices(symbols)
    strat = build(args.strategy, **parse_params(args.params))

    target = {k: v for k, v in strat.latest_weights(prices).items() if abs(v) > 1e-9}
    as_of = prices.index[-1].date()

    print("=" * 72)
    print(f"策略: {strat.label}")
    print(f"信号日期: {as_of}（基于本地缓存的最后一根K线）")
    print("目标权重:")
    for s, w in sorted(target.items(), key=lambda x: -x[1]):
        print(f"  {s:6} {w:>7.2%}")
    if not target:
        print("  （空仓）")
    print("=" * 72)

    # whatIf 单在线路上仍然要走 placeOrder（只是带了 whatIf 标记不成交），
    # 所以 --what-if 也不能用只读连接，否则请求会被券商侧挡掉。
    readonly = config.READONLY and not (args.execute or args.what_if)
    with IBConnection(readonly=readonly) as ib:
        nav = acct.account_summary(ib)["NetLiquidation"]
        pos_df = acct.positions_df(ib)
        current = dict(zip(pos_df["symbol"], pos_df["position"])) if not pos_df.empty else {}

        print(f"\n账户净值: ${nav:,.2f}")
        print(f"当前持仓: {current or '（空仓）'}\n")

        need = sorted(set(target) | set(current))
        fallback = {s: float(prices[s].iloc[-1]) for s in need if s in prices.columns}
        missing = [s for s in need if s not in fallback]
        if missing:
            print(f"⚠ 这些持仓不在标的池里，无法定价，将被忽略: {missing}")
            need = [s for s in need if s in fallback]

        px = live_prices(ib, need, fallback)

        plans = weights_to_orders(
            target_weights=target,
            current_positions=current,
            prices=px,
            nav=nav,
            min_trade_value=args.min_trade,
            drift_threshold=args.band,
        )

        result = execute_plan(
            ib, plans, nav=nav,
            dry_run=not (args.execute or args.what_if),
            what_if=args.what_if,
            order_type=args.order_type,
        )

        if args.execute and not result.empty:
            ib.sleep(2)
            acct.save_snapshot(ib)
            print("\n成交后的快照已更新，去 dashboard 里看。")

    if not (args.execute or args.what_if):
        print("\n这是预演。确认无误后加 --what-if 让 IBKR 校验一遍，再加 --execute 真下。")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError) as e:
        print(f"\n[中止] {e}", file=sys.stderr)
        raise SystemExit(1)
