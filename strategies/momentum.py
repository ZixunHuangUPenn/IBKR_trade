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
