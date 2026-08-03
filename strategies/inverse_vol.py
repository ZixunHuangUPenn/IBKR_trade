"""
逆波动率配置 —— 风险平价的简化版。

逻辑：按各标的近期波动率的倒数分配权重，波动小的多配。

为什么这件事有道理：
  等权组合看起来公平，其实不是。60/40 里股票占 60% 的资金，
  却贡献了 90% 以上的组合波动 —— 你以为自己配了债券，实际上还是在赌股票。
  按波动率倒数配权重，是让每个标的对组合风险的贡献大致相等。

  它比横截面动量更"笨"，但也因此更稳：不预测方向，只管理暴露。

为什么这里是"简化版"而不是真正的风险平价：
  真正的风险平价要用协方差矩阵，考虑标的之间的相关性。
  这里只用了各自的波动率，等于假设两两不相关 —— 这个假设在危机里
  恰好最不成立（危机中一切相关性趋近于 1）。所以它在你最需要分散的时候
  分散得最差。知道这一点，再决定要不要用它。

波动率目标（target_vol）这个开关：
  用组合的历史波动去缩放总仓位，把净值波动稳定在目标附近。
  波动率有聚集性（大波动跟着大波动），所以用历史波动预测近期波动是有效的 ——
  这是少数几个在样本外还站得住的规律之一。
  但它有代价：市场刚崩完的时候波动最高，仓位会被压到最低，
  于是你系统性地错过反弹的第一段。
"""

from __future__ import annotations

import pandas as pd

from backtest.engine import periodic_mask
from .base import Param, Strategy, register


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
