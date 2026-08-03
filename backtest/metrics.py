"""
绩效指标。

看指标的优先级（新手常常搞反）：
  1. 最大回撤 —— 决定你能不能拿得住。回撤 50% 的策略，你在第 30% 就会关掉它。
  2. 夏普     —— 单位风险的收益。夏普 1 已经不错，回测里超过 3 大概率是过拟合或有bug。
  3. 换手率   —— 隐藏的成本。年换手 2000% 的策略，手续费会吃掉大部分 alpha。
  4. 总收益   —— 最后才看。它最容易被杠杆和运气放大。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

import config

METRIC_LABELS = {
    "total_return": "总收益",
    "cagr": "年化收益",
    "ann_vol": "年化波动",
    "sharpe": "夏普比率",
    "sortino": "索提诺比率",
    "max_drawdown": "最大回撤",
    "calmar": "卡玛比率",
    "ann_turnover": "年化换手",
    "total_cost": "累计成本",
    "win_rate": "日胜率",
    "best_day": "最佳单日",
    "worst_day": "最差单日",
    "n_days": "交易日数",
}

PERCENT_METRICS = {"total_return", "cagr", "ann_vol", "max_drawdown",
                   "ann_turnover", "win_rate", "best_day", "worst_day", "total_cost"}


def drawdown_series(equity: pd.Series) -> pd.Series:
    """每一天相对历史最高点的回撤（负数）。"""
    return equity / equity.cummax() - 1.0


def max_drawdown(equity: pd.Series) -> float:
    return float(drawdown_series(equity).min())


def compute_metrics(
    returns: pd.Series,
    equity: pd.Series | None = None,
    turnover: pd.Series | None = None,
    costs: pd.Series | None = None,
    rf: float = 0.0,
    periods: int = config.TRADING_DAYS,
) -> dict[str, float]:
    r = returns.dropna()
    if len(r) < 2:
        return {k: float("nan") for k in METRIC_LABELS}

    if equity is None:
        equity = (1 + r).cumprod()

    years = len(r) / periods
    total = float(equity.iloc[-1] / equity.iloc[0] - 1)
    cagr = float((equity.iloc[-1] / equity.iloc[0]) ** (1 / years) - 1) if years > 0 else np.nan

    vol = float(r.std(ddof=1) * np.sqrt(periods))
    rf_daily = rf / periods
    excess = r - rf_daily
    sharpe = float(excess.mean() / r.std(ddof=1) * np.sqrt(periods)) if r.std(ddof=1) > 0 else np.nan

    downside = r[r < rf_daily]
    dvol = float(downside.std(ddof=1) * np.sqrt(periods)) if len(downside) > 1 else np.nan
    sortino = float(excess.mean() * periods / dvol) if dvol and dvol > 0 else np.nan

    mdd = max_drawdown(equity)
    calmar = float(cagr / abs(mdd)) if mdd < 0 else np.nan

    out = {
        "total_return": total,
        "cagr": cagr,
        "ann_vol": vol,
        "sharpe": sharpe,
        "sortino": sortino,
        "max_drawdown": mdd,
        "calmar": calmar,
        "ann_turnover": float(turnover.sum() / years) if turnover is not None and years else np.nan,
        "total_cost": float(costs.sum()) if costs is not None else np.nan,
        "win_rate": float((r > 0).mean()),
        "best_day": float(r.max()),
        "worst_day": float(r.min()),
        "n_days": float(len(r)),
    }
    return out


def format_metrics(m: dict[str, float]) -> pd.DataFrame:
    """转成人能读的表格。"""
    rows = []
    for key, label in METRIC_LABELS.items():
        v = m.get(key, float("nan"))
        if key == "n_days":
            s = f"{v:,.0f}"
        elif v != v:  # nan
            s = "—"
        elif key in PERCENT_METRICS:
            s = f"{v:.2%}"
        else:
            s = f"{v:.2f}"
        rows.append({"指标": label, "值": s})
    return pd.DataFrame(rows)


def compare(results: dict[str, dict[str, float]]) -> pd.DataFrame:
    """多个策略横向对比。"""
    df = pd.DataFrame(results).T
    df = df[[c for c in METRIC_LABELS if c in df.columns]]
    return df.rename(columns=METRIC_LABELS)
