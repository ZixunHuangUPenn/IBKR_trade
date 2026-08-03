"""
横截面动量 —— 学术界证据最扎实的异象之一。

逻辑：每月末，按过去 12 个月（跳过最近 1 个月）的收益给所有标的排序，
      买入排名前 N 的，等权持有一个月。

两个细节不是随便定的：
  跳过最近 1 个月   短期有反转效应，不跳过会明显拉低收益。
                    这就是文献里写作 "12-1 momentum" 的原因。
  月度调仓          日频调仓换手率会飙到几千个百分点，成本吃光一切。
                    动量信号本身变化很慢，没必要天天动。

加了一道绝对动量开关（time-series momentum）：
  如果一个标的自己的 12-1 收益是负的，即使排名靠前也不买。
  2008 那种全市场下跌里，这一条能救命 —— 否则你只是在"跌得最少的一堆里"满仓。
"""

from __future__ import annotations

import pandas as pd

from backtest.engine import periodic_mask
from .base import Param, Strategy, register


@register
class CrossSectionalMomentum(Strategy):
    name = "横截面动量"
    description = "按 12-1 月收益排序，月度持有最强的 N 个标的。"
    params_spec = [
        Param("lookback", "回看月数", 12, 3, 24, 1, "int"),
        Param("skip", "跳过最近月数", 1, 0, 3, 1, "int"),
        Param("top_n", "持有数量", 3, 1, 10, 1, "int"),
        Param("absolute_filter", "绝对动量过滤(1=开)", 1, 0, 1, 1, "int"),
    ]

    def generate_weights(self, prices: pd.DataFrame) -> pd.DataFrame:
        lb = int(self.lookback) * 21          # 一个月约 21 个交易日
        sk = int(self.skip) * 21
        top_n = min(int(self.top_n), prices.shape[1])

        # 过去 lb 个月到 sk 个月之前的累计收益
        past = prices.shift(sk)
        mom = past / past.shift(lb - sk) - 1.0

        # 排名：数值越大名次越靠前
        ranks = mom.rank(axis=1, ascending=False, na_option="keep")
        picked = (ranks <= top_n) & mom.notna()

        if int(self.absolute_filter):
            picked &= (mom > 0)

        n = picked.sum(axis=1)
        w = picked.astype(float).div(n.replace(0, pd.NA), axis=0)
        return w.fillna(0.0)     # 一个都没选中 = 全现金

    def rebalance_mask(self, index: pd.DatetimeIndex) -> pd.Series:
        return periodic_mask(index, "ME")


@register
class InverseVolatility(Strategy):
    name = "逆波动率配置"
    description = "按近期波动率倒数分配权重，波动小的多配。风险平价的简化版。"
    params_spec = [
        Param("vol_window", "波动率窗口(日)", 60, 20, 250, 5, "int"),
        Param("target_vol", "组合目标年化波动(%)", 10, 0, 30, 1, "int"),
    ]

    def generate_weights(self, prices: pd.DataFrame) -> pd.DataFrame:
        win = int(self.vol_window)
        rets = prices.pct_change(fill_method=None)
        vol = rets.rolling(win, min_periods=win).std()

        inv = 1.0 / vol.replace(0, pd.NA)
        w = inv.div(inv.sum(axis=1), axis=0)

        target = float(self.target_vol) / 100.0
        if target > 0:
            # 用组合历史波动缩放总仓位，把净值波动稳定在目标附近。
            # 上限 1.0：不加杠杆。想开杠杆就把 clip 上界改掉，但先想清楚风险。
            port_vol = (w.shift(1) * rets).sum(axis=1).rolling(win, min_periods=win).std()
            scale = (target / (port_vol * (252 ** 0.5))).clip(upper=1.0)
            w = w.mul(scale, axis=0)

        return w.fillna(0.0)

    def rebalance_mask(self, index: pd.DatetimeIndex) -> pd.Series:
        return periodic_mask(index, "ME")


@register
class FixedWeights(Strategy):
    """
    固定权重再平衡 —— 你现有的 rebalancer.py 的回测版本。

    别小看它。60/40 这类静态组合是绝大多数主动策略真正的对手，
    而且它的换手率极低、容量无限、不会失效。
    """
    name = "固定权重再平衡"
    description = "维持一组固定的目标权重，定期调回。静态资产配置。"
    params_spec = [
        Param("rebalance_months", "调仓间隔(月)", 3, 1, 12, 1, "int"),
    ]

    def __init__(self, weights: dict[str, float] | None = None, **params):
        super().__init__(**params)
        self.weights = weights          # None = 等权

    def generate_weights(self, prices: pd.DataFrame) -> pd.DataFrame:
        w = pd.DataFrame(0.0, index=prices.index, columns=prices.columns)
        if self.weights:
            missing = set(self.weights) - set(prices.columns)
            if missing:
                raise KeyError(f"价格表里没有这些标的: {missing}")
            for sym, wt in self.weights.items():
                w[sym] = wt
        else:
            w.loc[:, :] = 1.0 / prices.shape[1]
        return w

    def rebalance_mask(self, index: pd.DatetimeIndex) -> pd.Series:
        m = int(self.rebalance_months)
        freq = "ME" if m == 1 else f"{m}ME"
        return periodic_mask(index, freq)
