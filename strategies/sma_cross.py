"""
双均线趋势跟踪 —— 教学用的第一个策略。

逻辑：短均线在长均线之上 = 上升趋势 = 持有；否则空仓拿现金。

为什么拿它当第一课，而不是因为它好用：
  它是最容易看清"信号 -> 权重 -> 成本 -> 净值"这条链路的例子。
  你会亲眼看到几件事：
    - 把 lag 从 1 改成 0（允许未来函数），夏普会凭空变好一大截
    - 把 rebalance_band 从 0 调到 0.05，换手率掉一半，收益几乎不变
    - 参数从 (20,60) 换成 (21,63)，结果差很多 —— 这说明它在过拟合边缘

真实结论：单标的双均线长期很难打赢买入持有，它主要的价值是降低回撤。
先接受这个事实，再去想怎么改进。
"""

from __future__ import annotations

import pandas as pd

from .base import Param, Strategy, register


@register
class SMACross(Strategy):
    name = "双均线趋势"
    description = "短均线上穿长均线时持有，否则空仓。经典趋势跟踪入门。"
    params_spec = [
        Param("fast", "快线周期", 20, 5, 100, 1, "int"),
        Param("slow", "慢线周期", 100, 20, 300, 5, "int"),
        Param("gross", "满仓时总仓位", 1.0, 0.1, 1.0, 0.05, "float"),
    ]

    def generate_weights(self, prices: pd.DataFrame) -> pd.DataFrame:
        fast, slow = int(self.fast), int(self.slow)
        if fast >= slow:
            raise ValueError(f"快线({fast})必须短于慢线({slow})")

        ma_fast = prices.rolling(fast, min_periods=fast).mean()
        ma_slow = prices.rolling(slow, min_periods=slow).mean()

        # 注意：rolling 用的是截至 t 日（含）的数据，符合基类约定
        signal = (ma_fast > ma_slow).astype(float)

        n_active = signal.sum(axis=1)
        w = signal.div(n_active.replace(0, pd.NA), axis=0) * float(self.gross)
        return w.fillna(0.0)
