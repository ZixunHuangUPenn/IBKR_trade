"""
策略基类。

核心约定 —— 所有策略都归结为同一件事：

    输入：价格表 (日期 x 标的)
    输出：目标权重表 (日期 x 标的)

    第 t 行的权重，只能用 t 日收盘及以前的数据算出来。
    回测引擎会自动 shift(1) 把它推迟一天执行；实盘则取最后一行去下单。

把"择时/选股/仓位管理"统一成目标权重，好处是回测和实盘用的是同一个函数，
不会出现"回测里是这么算的，实盘代码里又是另一套"这种最难查的 bug。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import pandas as pd

from backtest.engine import BacktestResult, run_backtest


@dataclass
class Param:
    """参数元信息，让 dashboard 能自动生成控件。"""
    name: str
    label: str
    default: float
    min: float
    max: float
    step: float = 1
    kind: str = "int"          # int | float


class Strategy(ABC):
    name: str = "strategy"
    description: str = ""
    params_spec: list[Param] = []

    def __init__(self, **params):
        defaults = {p.name: p.default for p in self.params_spec}
        unknown = set(params) - set(defaults)
        if unknown:
            raise TypeError(f"{self.name} 不认识的参数: {unknown}")
        self.params = {**defaults, **params}

    def __getattr__(self, item):
        # 让 self.fast 直接取到 self.params['fast']
        try:
            return self.__dict__["params"][item]
        except KeyError:
            raise AttributeError(item) from None

    @abstractmethod
    def generate_weights(self, prices: pd.DataFrame) -> pd.DataFrame:
        """返回与 prices 同形状的目标权重表。"""

    def rebalance_mask(self, index: pd.DatetimeIndex) -> pd.Series | None:
        """哪些日期允许调仓。None = 每天都可以（受 rebalance_band 约束）。"""
        return None

    def backtest(self, prices: pd.DataFrame, **kw) -> BacktestResult:
        w = self.generate_weights(prices)
        kw.setdefault("rebalance_mask", self.rebalance_mask(prices.index))
        kw.setdefault("name", self.label)
        return run_backtest(prices, w, **kw)

    def latest_weights(self, prices: pd.DataFrame) -> dict[str, float]:
        """取最后一行 —— 这就是今天收盘后该持有的仓位，直接喂给下单模块。"""
        w = self.generate_weights(prices)
        return w.iloc[-1].to_dict()

    @property
    def label(self) -> str:
        """只显示被改过的参数，默认值不占地方，对比表才看得清。"""
        defaults = {p.name: p.default for p in self.params_spec}
        changed = {k: v for k, v in self.params.items() if v != defaults.get(k)}
        if not changed:
            return self.name
        kv = ",".join(f"{k}={v}" for k, v in changed.items())
        return f"{self.name}({kv})"


# ---------------- 注册表 ----------------
REGISTRY: dict[str, type[Strategy]] = {}


def register(cls: type[Strategy]) -> type[Strategy]:
    REGISTRY[cls.name] = cls
    return cls


# ---------------- 权重后处理工具 ----------------

def normalize(w: pd.DataFrame, gross: float = 1.0) -> pd.DataFrame:
    """按行归一化到指定总仓位。全零的行保持全零（空仓）。"""
    s = w.abs().sum(axis=1)
    out = w.div(s.replace(0, pd.NA), axis=0) * gross
    return out.fillna(0.0)


def cap_weights(w: pd.DataFrame, max_weight: float) -> pd.DataFrame:
    """单标的权重上限。防止某一个品种主导整个组合。"""
    return w.clip(upper=max_weight, lower=-max_weight)
