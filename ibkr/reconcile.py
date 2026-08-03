"""
对账 —— 确认"实际持仓"和"你以为的持仓"是同一回事。

为什么这件事在 Paper 上无所谓、在实盘上是必须的：

Paper 账户里限价单没成交、只成交一半、超时还挂着，后果顶多是少赚点。
实盘上这意味着**你的风险敞口和你以为的不一样，而且你不知道**。
下单函数返回了不等于仓位到位了 —— 这两件事之间隔着一整个订单状态机。

对账要回答三个问题：
  1. 还有没有挂着没成的单？（有 = 仓位还在变，现在的持仓不是最终状态）
  2. 实际权重和目标权重差多少？
  3. 差得能不能接受？

设计上刻意不自动补单。发现偏差先告警、让人看一眼 ——
自动补单的循环一旦写错（比如价格取错导致反复下单），损失是不封顶的。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

import pandas as pd
from ib_async import IB

import config
from . import account as acct

log = logging.getLogger("ibkr.reconcile")


def _finite(x, default: float = float("nan")) -> float:
    """IBKR 的字段经常给 nan / None / '' —— 统一收口，别让 nan 泄进算术里。"""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


@dataclass
class ReconcileReport:
    ok: bool
    nav: float
    table: pd.DataFrame                       # symbol/target_w/actual_w/drift/shares/value
    open_orders: pd.DataFrame
    problems: list[str] = field(default_factory=list)

    def text(self) -> str:
        lines = [f"净值 ${self.nav:,.2f}"]
        if self.table.empty:
            lines.append("（空仓，且无目标持仓）")
        else:
            lines.append(f"{'标的':<8}{'目标':>9}{'实际':>9}{'偏差':>9}{'股数':>9}")
            for r in self.table.itertuples():
                lines.append(f"{r.symbol:<8}{r.target_w:>8.2%} {r.actual_w:>8.2%} "
                             f"{r.drift:>+8.2%} {r.shares:>9,.0f}")
        if not self.open_orders.empty:
            lines.append(f"\n未成交订单 {len(self.open_orders)} 笔：")
            for r in self.open_orders.itertuples():
                lines.append(f"  {r.action} {r.symbol} x{r.quantity} "
                             f"@{r.lmtPrice} 状态={r.status} 已成={r.filled}")
        if self.problems:
            lines.append("\n对账不通过：")
            lines += [f"  ✗ {p}" for p in self.problems]
        else:
            lines.append("\n对账通过。")
        return "\n".join(lines)


def reconcile(
    ib: IB,
    target_weights: dict[str, float],
    prices: dict[str, float] | None = None,
    tolerance: float = None,
) -> ReconcileReport:
    """
    比对实际持仓与目标权重。

    prices     用于给持仓估值。不传就用 IBKR 返回的市值；
               但 positions() 回退路径拿不到市值，所以调用方手上有实时价的话务必传进来。
    tolerance  单标的绝对权重偏差上限，默认 config.RECONCILE_TOLERANCE
    """
    tol = config.RECONCILE_TOLERANCE if tolerance is None else tolerance
    prices = prices or {}
    problems: list[str] = []

    summary = acct.account_summary(ib)
    nav = _finite(summary.get("NetLiquidation"), 0.0)
    pos = acct.positions_df(ib)
    open_orders = acct.open_orders_df(ib)

    if nav <= 0:
        problems.append(f"净值异常：{nav}")

    # 实际持仓：股数 + 市值。市值优先用 IBKR 的，拿不到就用传进来的价格自己算。
    held: dict[str, tuple[float, float]] = {}
    for r in pos.itertuples():
        shares = _finite(r.position, 0.0)
        value = _finite(r.market_value)
        if not math.isfinite(value):
            px = _finite(prices.get(r.symbol), _finite(r.market_price))
            value = shares * px if math.isfinite(px) else float("nan")
        held[r.symbol] = (shares, value)

    rows = []
    for sym in sorted(set(target_weights) | set(held)):
        shares, value = held.get(sym, (0.0, 0.0))
        target_w = float(target_weights.get(sym, 0.0))

        if not math.isfinite(value):
            # 估不出值就不能声称"对上了"。宁可报警，也不要假装通过。
            problems.append(f"{sym}: 持有 {shares:,.0f} 股但估不出市值（无价格），无法对账")
            actual_w, drift = float("nan"), float("nan")
        else:
            actual_w = value / nav if nav > 0 else 0.0
            drift = actual_w - target_w
            if abs(drift) > tol:
                problems.append(
                    f"{sym}: 实际 {actual_w:.2%} vs 目标 {target_w:.2%}"
                    f"（偏差 {drift:+.2%}，超过容忍度 {tol:.2%}）"
                )

        rows.append({
            "symbol": sym,
            "target_w": target_w,
            "actual_w": actual_w,
            "drift": drift,
            "shares": shares,
            "value": value,
        })

    if not open_orders.empty:
        problems.append(
            f"还有 {len(open_orders)} 笔未成交订单挂着 —— "
            f"当前持仓不是最终状态，上面的偏差数字只是快照"
        )

    table = pd.DataFrame(
        rows, columns=["symbol", "target_w", "actual_w", "drift", "shares", "value"]
    )
    if not table.empty:
        table = table.sort_values("target_w", ascending=False, ignore_index=True)

    report = ReconcileReport(
        ok=not problems, nav=nav, table=table,
        open_orders=open_orders, problems=problems,
    )
    if report.ok:
        log.info("对账通过：%d 个标的，最大偏差 %.2f%%", len(table),
                 100 * (table["drift"].abs().max() if not table.empty else 0.0))
    else:
        for p in problems:
            log.warning("对账: %s", p)
    return report
