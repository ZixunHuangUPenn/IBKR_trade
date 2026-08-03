"""
策略库。import 本包即完成注册，之后用 REGISTRY 按名字取。

一个策略一个文件。新增策略时在这里 import 一行 —— 注册是 import 的副作用
（@register 装饰器），漏了这一行的表现是 REGISTRY 里查不到它，
而不是报错，所以容易查半天。
"""

from .base import Param, Strategy, REGISTRY, register, normalize, cap_weights
from .sma_cross import SMACross
from .momentum import CrossSectionalMomentum
from .inverse_vol import InverseVolatility
from .fixed_weights import FixedWeights

__all__ = [
    "Param", "Strategy", "REGISTRY", "register", "normalize", "cap_weights",
    "SMACross", "CrossSectionalMomentum", "InverseVolatility", "FixedWeights",
]


def build(name: str, **params) -> Strategy:
    if name not in REGISTRY:
        raise KeyError(f"没有名为 {name!r} 的策略。可用：{list(REGISTRY)}")
    return REGISTRY[name](**params)
