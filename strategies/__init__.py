"""策略库。import 本包即完成注册，之后用 REGISTRY 按名字取。"""

from .base import Param, Strategy, REGISTRY, register, normalize, cap_weights
from .sma_cross import SMACross
from .momentum import CrossSectionalMomentum, InverseVolatility, FixedWeights

__all__ = [
    "Param", "Strategy", "REGISTRY", "register", "normalize", "cap_weights",
    "SMACross", "CrossSectionalMomentum", "InverseVolatility", "FixedWeights",
]


def build(name: str, **params) -> Strategy:
    if name not in REGISTRY:
        raise KeyError(f"没有名为 {name!r} 的策略。可用：{list(REGISTRY)}")
    return REGISTRY[name](**params)
