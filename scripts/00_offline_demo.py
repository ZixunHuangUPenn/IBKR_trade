"""
第 0 课：不连 IBKR，先把回测链路跑通。

用合成数据（几何布朗运动 + 一点趋势/相关性）造出一批"假 ETF"，
跑完整的 策略 -> 回测 -> 指标 -> 对比 流程。

目的：在你还没搞定 TWS/Gateway 之前，先理解这套代码的骨架。
      也是本项目的冒烟测试 —— 它跑通了，说明环境没问题。

    python scripts/00_offline_demo.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

import config
from backtest.engine import buy_and_hold
from backtest.metrics import compare, format_metrics
from strategies import CrossSectionalMomentum, FixedWeights, InverseVolatility, SMACross


def synthetic_prices(n_days: int = 2600, seed: int = 42) -> pd.DataFrame:
    """
    造 6 个相关的资产。参数是拍脑袋的，但量级贴近真实：
    年化收益 2%~10%，年化波动 10%~28%，彼此有正相关（市场因子）。
    """
    rng = np.random.default_rng(seed)
    names = ["STK_US", "STK_INTL", "STK_EM", "BOND", "GOLD", "REIT"]
    mu = np.array([0.09, 0.06, 0.05, 0.02, 0.04, 0.07]) / config.TRADING_DAYS
    sig = np.array([0.16, 0.18, 0.24, 0.06, 0.15, 0.22]) / np.sqrt(config.TRADING_DAYS)
    beta = np.array([1.0, 0.9, 1.1, -0.1, 0.2, 0.8])   # 对市场因子的敏感度

    market = rng.standard_normal(n_days)
    idio = rng.standard_normal((n_days, len(names)))
    z = beta * market[:, None] + idio * 0.8

    # 去均值再归一化。不去均值的话，2600 个样本的抽样误差会让实际漂移
    # 和上面设定的 mu 差出好几个百分点 —— 演示数据里不该有这种噪音。
    z = (z - z.mean(axis=0)) / z.std(axis=0)

    rets = mu + sig * z
    dates = pd.bdate_range("2015-01-01", periods=n_days)
    prices = pd.DataFrame(100 * np.exp(np.cumsum(rets, axis=0)), index=dates, columns=names)
    return prices


def save_as_cache(prices: pd.DataFrame, seed: int = 7) -> None:
    """
    把合成数据写成和真实行情一样格式的 parquet，好让 dashboard 和
    03_run_backtest.py 在没有 IBKR 连接时也能跑通。
    加 SYNTH_ 前缀，免得哪天把假数据当成真数据用了。
    """
    rng = np.random.default_rng(seed)
    for col in prices.columns:
        close = prices[col]
        # 由收盘价反推一组自洽的 OHLC：日内振幅按当日波动的量级来
        noise = np.abs(rng.normal(0, 0.006, len(close)))
        df = pd.DataFrame({
            "open": close.shift(1).fillna(close.iloc[0]) * (1 + rng.normal(0, 0.002, len(close))),
            "high": close * (1 + noise),
            "low": close * (1 - noise),
            "close": close,
            "volume": rng.integers(1_000_000, 9_000_000, len(close)).astype(float),
        }, index=close.index)
        df["high"] = df[["open", "high", "close"]].max(axis=1)
        df["low"] = df[["open", "low", "close"]].min(axis=1)
        df.index.name = "date"
        df.to_parquet(config.BARS_DIR / f"SYNTH_{col}_1day.parquet")
    print(f"合成数据已写入缓存 {config.BARS_DIR}（前缀 SYNTH_）")


def main() -> int:
    save_cache = "--save-cache" in sys.argv
    prices = synthetic_prices()
    print(f"合成数据: {prices.shape[0]} 个交易日 x {prices.shape[1]} 个标的")
    print(f"区间: {prices.index[0].date()} ~ {prices.index[-1].date()}\n")

    runs = [
        buy_and_hold(prices),
        buy_and_hold(prices, "STK_US"),
        FixedWeights({"STK_US": 0.6, "BOND": 0.4}, rebalance_months=3).backtest(prices),
        SMACross(fast=20, slow=100).backtest(prices, rebalance_band=0.05),
        CrossSectionalMomentum(top_n=2).backtest(prices),
        InverseVolatility(target_vol=10).backtest(prices),
    ]

    print("=" * 100)
    for r in runs:
        print(r.summary())
    print("=" * 100)

    # 未来函数对照实验：同一个策略，唯一区别是 lag=0（今天的信号今天就执行）
    honest = SMACross(fast=20, slow=100).backtest(prices, rebalance_band=0.05)
    cheat = SMACross(fast=20, slow=100).backtest(prices, rebalance_band=0.05, lag=0)
    print("\n未来函数的代价（同一策略，只改 lag）：")
    print(f"  lag=1 诚实版: 年化 {honest.metrics['cagr']:.2%}  夏普 {honest.metrics['sharpe']:.2f}")
    print(f"  lag=0 作弊版: 年化 {cheat.metrics['cagr']:.2%}  夏普 {cheat.metrics['sharpe']:.2f}")
    print("  差距全部来自'用了当天收盘价才知道的信息'。自己写回测时最常见的 bug 就是这个。")

    # 交易成本的代价
    free = SMACross(fast=20, slow=100).backtest(
        prices, rebalance_band=0.0, commission_bps=0, slippage_bps=0)
    real = SMACross(fast=20, slow=100).backtest(prices, rebalance_band=0.0)
    print("\n交易成本的代价（同一策略，改成本和调仓阈值）：")
    print(f"  零成本 band=0   : 年化 {free.metrics['cagr']:>7.2%}  "
          f"年换手 {free.metrics['ann_turnover']:>6.0%}  累计成本 {free.metrics['total_cost']:.2%}")
    print(f"  真成本 band=0   : 年化 {real.metrics['cagr']:>7.2%}  "
          f"年换手 {real.metrics['ann_turnover']:>6.0%}  累计成本 {real.metrics['total_cost']:.2%}")
    print(f"  真成本 band=0.05: 年化 {honest.metrics['cagr']:>7.2%}  "
          f"年换手 {honest.metrics['ann_turnover']:>6.0%}  累计成本 {honest.metrics['total_cost']:.2%}")
    print("  第一行和第二行的差 = 成本吃掉的收益。")
    print("  第二行和第三行的差 = 加个不调仓缓冲区能省回多少。")
    print("  注意双均线是 0/1 的离散信号，band 只能过滤漂移、拦不住信号翻转，")
    print("  所以这里换手降得有限。对连续权重的策略（比如逆波动率）效果会明显得多。")

    print("\n" + "=" * 100)
    print("详细指标对比：")
    print(compare({r.name: r.metrics for r in runs}).to_string(
        float_format=lambda x: f"{x:.3f}"))

    out = config.RESULTS_DIR / "offline_demo_equity.csv"
    pd.DataFrame({r.name: r.equity for r in runs}).to_csv(out, encoding="utf-8-sig")
    print(f"\n净值曲线已存到 {out}")

    if save_cache:
        save_as_cache(prices)
        print("现在可以直接跑 `streamlit run dashboard/app.py` 试试可视化平台。")
    else:
        print("加 --save-cache 可把合成数据写进行情缓存，"
              "这样不连 IBKR 也能试 dashboard。")

    print("下一步: python scripts/01_test_connection.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
