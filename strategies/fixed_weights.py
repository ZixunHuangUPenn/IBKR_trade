"""
固定权重再平衡 —— 静态资产配置。也是根目录 rebalancer.py 的回测版本。

逻辑：维持一组固定的目标权重，定期调回。没有择时，没有选股。

别小看它。这是你所有主动策略真正的对手：

  - 换手率极低，成本几乎可以忽略
  - 容量无限，多少钱都能跑
  - 不会失效 —— 它没有依赖任何一个可能被套利掉的规律
  - 再平衡本身就是一种收益来源：强制你卖掉涨多的、买入跌多的

绝大多数人的长期收益来自"配了什么"，而不是"什么时候买卖"。
资产配置的贡献远大于择时和选股，但新手的注意力刚好相反 ——
90% 花在调策略参数上，0% 花在想清楚股债比例上。

所以在写任何主动策略之前，先用它跑一遍 60/40，把那条净值曲线记在心里。
你后面做的每一件事，都要能解释清楚为什么值得比它多承担一份复杂度。

调仓频率的取舍：
  调得太勤，成本吃掉再平衡收益；调得太懒，权重漂移到你没打算承担的风险上。
  季度（默认）到年度之间通常是合理区间，而且这个参数对结果的影响
  远小于"你一开始配了什么" —— 这本身就是个值得体会的事实。
"""

from __future__ import annotations

import pandas as pd

from backtest.engine import periodic_mask
from .base import Param, Strategy, register


@register
class FixedWeights(Strategy):
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
