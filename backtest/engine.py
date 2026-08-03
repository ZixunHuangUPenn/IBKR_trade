"""
向量化组合回测引擎。

它做对的四件事（大多数新手自己写的回测都会在这里出错）：

1) 杜绝未来函数
   策略输出的第 t 行权重，只允许用到 t 日收盘及之前的信息。
   引擎强制 shift(lag)：t 日算出的信号，t+1 日才生效。
   这一个 shift 常常能把"年化 80%"变成"年化 6%"。

2) 权重会漂移
   买入 60/40 之后不管它，股票涨了权重就变成 65/35。
   只在真正调仓的那天才产生换手和成本，而不是每天假装重置一次。

3) 成本按换手计
   成本 = sum|目标权重 - 当前权重| x 单边费率。
   手续费和滑点分开设 —— 它们量级完全不同，滑点通常大得多。

4) 空仓部分算现金
   权重和小于 1 时，剩下的是现金（收益 = cash_rate），
   不会凭空消失，也不会被自动加杠杆。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

import config
from .metrics import compute_metrics, drawdown_series

log = logging.getLogger("backtest.engine")


@dataclass
class BacktestResult:
    equity: pd.Series               # 净值曲线（起点 = initial_capital）
    returns: pd.Series              # 日收益
    held_weights: pd.DataFrame      # 每日实际持仓权重（含漂移）
    target_weights: pd.DataFrame    # 策略目标权重（已按 lag 平移）
    turnover: pd.Series             # 每日换手（双边合计）
    costs: pd.Series                # 每日成本（占净值比例）
    metrics: dict = field(default_factory=dict)
    name: str = "strategy"

    @property
    def drawdown(self) -> pd.Series:
        return drawdown_series(self.equity)

    def summary(self) -> str:
        m = self.metrics
        return (f"{self.name:<24} 年化 {m['cagr']:>7.2%} | 波动 {m['ann_vol']:>6.2%} | "
                f"夏普 {m['sharpe']:>5.2f} | 最大回撤 {m['max_drawdown']:>7.2%} | "
                f"年换手 {m['ann_turnover']:>6.0%}")


def run_backtest(
    prices: pd.DataFrame,
    target_weights: pd.DataFrame,
    initial_capital: float = config.INITIAL_CAPITAL,
    commission_bps: float = config.COMMISSION_BPS,
    slippage_bps: float = config.SLIPPAGE_BPS,
    rebalance_band: float = 0.0,
    rebalance_mask: pd.Series | None = None,
    cash_rate: float = 0.0,
    lag: int = 1,
    name: str = "strategy",
) -> BacktestResult:
    """
    prices          index=日期, columns=标的, values=（复权）收盘价
    target_weights  同形状。第 t 行 = 用 t 日收盘信息算出的目标权重。

    rebalance_band  总偏离 sum|Δw| 低于此值就不调仓。
                    0 = 每天调回目标；0.05~0.10 通常能砍掉大半成本而几乎不影响收益。
    rebalance_mask  只在为 True 的日期允许调仓（月度/周度调仓用）。
                    与 band 是"与"关系：两个条件都满足才动手。
    lag             信号到执行的延迟（交易日）。1 = t 日收盘算、t+1 日持有。
                    改成 0 就是故意引入未来函数，只用于对照实验。
    """
    prices = prices.sort_index()
    target_weights = (target_weights
                      .reindex(prices.index)
                      .reindex(columns=prices.columns)
                      .fillna(0.0))

    if (target_weights.abs().sum(axis=1) > 3).any():
        log.warning("某些日期总权重 > 3 倍净值，确认这是你想要的杠杆。")

    tgt = target_weights.shift(lag).fillna(0.0)          # 关键：延迟执行
    rets = prices.pct_change(fill_method=None).fillna(0.0)
    cost_rate = (commission_bps + slippage_bps) / 10_000.0

    dates = prices.index
    n, k = len(dates), prices.shape[1]

    R = rets.to_numpy(dtype=float)
    W = tgt.to_numpy(dtype=float)
    if rebalance_mask is None:
        M = np.ones(n, dtype=bool)
    else:
        M = rebalance_mask.reindex(dates).fillna(False).to_numpy(dtype=bool)

    equity = np.empty(n)
    held = np.zeros((n, k))
    turn = np.zeros(n)
    cost = np.zeros(n)

    w_prev = np.zeros(k)
    cash_prev = 1.0
    eq = float(initial_capital)
    daily_cash = cash_rate / config.TRADING_DAYS

    for i in range(n):
        # 1) 价格变动 -> 仓位价值漂移
        val_assets = w_prev * (1.0 + R[i])
        val_cash = cash_prev * (1.0 + daily_cash)
        growth = val_assets.sum() + val_cash
        eq *= growth
        w_drift = val_assets / growth if growth > 0 else val_assets

        # 2) 对比目标，决定是否调仓
        diff = float(np.abs(W[i] - w_drift).sum())
        if M[i] and diff > rebalance_band:
            c = diff * cost_rate
            eq *= (1.0 - c)
            turn[i], cost[i] = diff, c
            w_prev = W[i].copy()
        else:
            w_prev = w_drift

        cash_prev = 1.0 - w_prev.sum()
        equity[i] = eq
        held[i] = w_prev

    eq_s = pd.Series(equity, index=dates, name="equity")
    ret_s = eq_s.pct_change(fill_method=None).fillna(0.0)
    turn_s = pd.Series(turn, index=dates, name="turnover")
    cost_s = pd.Series(cost, index=dates, name="cost")

    return BacktestResult(
        equity=eq_s,
        returns=ret_s,
        held_weights=pd.DataFrame(held, index=dates, columns=prices.columns),
        target_weights=tgt,
        turnover=turn_s,
        costs=cost_s,
        metrics=compute_metrics(ret_s, eq_s, turn_s, cost_s),
        name=name,
    )


def buy_and_hold(prices: pd.DataFrame, symbol: str | None = None, **kw) -> BacktestResult:
    """
    基准。任何策略都必须先打败它 —— 打不过就别折腾了，直接买指数。

    真·买入持有：只在第一天建仓，之后一次都不调。
    """
    w = pd.DataFrame(0.0, index=prices.index, columns=prices.columns)
    if symbol:
        if symbol not in prices.columns:
            raise KeyError(f"{symbol} 不在价格表里：{list(prices.columns)}")
        w[symbol] = 1.0
        label = f"买入持有 {symbol}"
    else:
        w.loc[:, :] = 1.0 / prices.shape[1]
        label = "等权买入持有"

    lag = kw.get("lag", 1)
    mask = pd.Series(False, index=prices.index)
    if len(mask) > lag:
        mask.iloc[lag] = True        # 只有建仓那一天允许交易

    kw.setdefault("name", label)
    return run_backtest(prices, w, rebalance_mask=mask, **kw)


def periodic_mask(index: pd.DatetimeIndex, freq: str = "ME") -> pd.Series:
    """
    生成"每月/每周最后一个交易日"的调仓掩码。
    freq: 'ME'=月末, 'W-FRI'=每周五, 'QE'=季末
    """
    s = pd.Series(index, index=index)
    marks = s.groupby(pd.Grouper(freq=freq)).max().dropna()
    mask = pd.Series(False, index=index)
    mask.loc[marks.values] = True
    return mask
