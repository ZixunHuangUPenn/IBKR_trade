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

import logging

import pandas as pd

from backtest.engine import periodic_mask
from .base import Param, Strategy, register

log = logging.getLogger("strategies.fixed_weights")

# 权重和允许偏离 1.0 多少还算"就是想配满"。留 0.5 个百分点吸收 1/3 这类除不尽的写法。
_SUM_TOL = 0.005


def parse_weights(spec: str) -> dict[str, float]:
    """
    把 "QQQ:0.5,SPY:0.3,IWM:0.2" 解析成 {"QQQ": 0.5, "SPY": 0.3, "IWM": 0.2}。

    为什么需要它：命令行只能传字符串。没有这个入口，固定权重策略就只能在
    Python 里和 dashboard 里用，永远上不了 05_daily_job.py 的定时任务 ——
    而"定好比例、定期调回"恰恰是最该交给机器无人值守跑的那一类策略。

    也接受百分号写法 "QQQ:50%,SPY:30%,IWM:20%"，因为人是按 50/30/20 想的，
    每次在脑子里除以 100 迟早会错一次。

    但**不**做"数字大于 1 就当成百分数"的自动猜测：那样 "QQQ:1,SPY:1"
    到底是两个满仓还是各占一半，只有写的人知道 —— 而这个歧义最后是用
    真金白银兑现的。宁可让它报错。
    """
    weights: dict[str, float] = {}
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        if ":" not in item:
            raise ValueError(
                f"权重格式应为 代码:权重，收到 {item!r}。\n"
                f"完整例子：weights=QQQ:0.5,SPY:0.3,IWM:0.2（或 QQQ:50%,SPY:30%,IWM:20%）"
            )
        sym, raw = item.split(":", 1)
        sym, raw = sym.strip().upper(), raw.strip()
        pct = raw.endswith("%")
        try:
            val = float(raw.rstrip("%"))
        except ValueError:
            raise ValueError(f"{sym} 的权重 {raw!r} 不是数字") from None
        if sym in weights:
            raise ValueError(f"{sym} 在权重表里出现了两次")
        weights[sym] = val / 100 if pct else val

    if not weights:
        raise ValueError(f"权重表是空的: {spec!r}")

    total = sum(weights.values())
    if total > 1 + _SUM_TOL:
        hint = ""
        if abs(total - 100) < 1:
            # 最常见的一次性事故：把 50/30/20 直接填进去了。
            # 它不会在解析这一步显形，会一路走到下单量算成 100 倍。
            hint = ("\n看起来你写的是百分数。要么改成小数（0.5），"
                    "要么给每个数加 % 号（50%）。")
        raise ValueError(
            f"权重之和 = {total:.4g}，超过 1.0。这个组合是加了杠杆的，"
            f"固定权重策略不支持。{hint}"
        )
    if total < 1 - _SUM_TOL:
        # 合法 —— 剩下的就是现金。但必须说出来，否则"少打了一位"和"故意留现金"
        # 长得一模一样，而前者会让你以为自己满仓了。
        log.warning("权重之和 = %.2f%%，不足 100%%，差额 %.2f%% 将以现金持有。",
                    total * 100, (1 - total) * 100)
    return weights


@register
class FixedWeights(Strategy):
    name = "固定权重再平衡"
    description = "维持一组固定的目标权重，定期调回。静态资产配置。"
    params_spec = [
        Param("rebalance_months", "调仓间隔(月)", 3, 1, 12, 1, "int"),
    ]

    def __init__(self, weights: dict[str, float] | str | None = None, **params):
        super().__init__(**params)
        # dict 来自 Python / dashboard，str 来自命令行的 --params weights=...
        self.weights = parse_weights(weights) if isinstance(weights, str) else weights
        # None = 等权

    @property
    def label(self) -> str:
        """
        把权重也写进 label。

        基类只显示"被改过的参数"，而 weights 不在 params_spec 里 —— 于是两个
        完全不同的配置在日志、通知和回测对比图里都叫"固定权重再平衡"，
        分不出谁是谁。持仓比例正是这个策略唯一的内容，不能不显示。
        """
        base = super().label
        if not self.weights:
            return base
        kv = "/".join(f"{s}{w:.0%}" for s, w in
                      sorted(self.weights.items(), key=lambda x: -x[1]))
        return f"{base}[{kv}]"

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
