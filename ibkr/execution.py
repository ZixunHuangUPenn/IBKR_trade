"""
下单执行。

这一层的目标不是"下得快"，是"下错的时候能拦住、下完了能说清发生了什么"。

三层保护，从松到紧：
  1. DRY_RUN       —— 只打印，不发送
  2. whatIf 预演   —— 真的发给 IBKR，但让它只回margin影响、不成交（最真实的演练）
  3. readonly 连接 —— 从 socket 层面就不允许下单
另外有单笔金额上限和总换手上限，防止代码写错导致的荒唐订单。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import pandas as pd
from ib_async import IB, LimitOrder, MarketOrder, Order

import config
from .market_data import make_contract

log = logging.getLogger("ibkr.execution")

# 熔断阈值：超过就拒绝执行整个计划，而不是"跳过这一单"。
# 单笔异常通常意味着上游算错了，这时候执行剩下的单更危险。
#
# 阈值按净值比例而非固定金额 —— 固定金额不知道你的账户是 1 万还是 1000 万，
# 设小了会拦住正常的首次建仓（60/40 里股票那一腿本来就是净值的 60%），
# 设大了对小账户又形同虚设。
#
# 这道闸真正要拦的是**代码 bug**：数量算错 100 倍、价格取成 0、
# 权重写成 50 而不是 0.5 —— 这类错误产生的订单都是净值的若干倍。
MAX_ORDER_PCT_NAV = 1.0      # 单笔不得超过净值的 100%（无杠杆时买不了更多）
MAX_TOTAL_TURNOVER = 2.0     # 总换手上限。2.0 = 全部清仓再全部换新，已是极限
MAX_ORDER_VALUE = None       # 可选的绝对金额上限；None = 不限。想加保险就设个数


@dataclass
class OrderPlan:
    symbol: str
    action: str          # BUY / SELL
    quantity: int
    ref_price: float     # 用于估算金额和限价的参考价
    reason: str = ""

    @property
    def notional(self) -> float:
        return self.quantity * self.ref_price

    def __str__(self) -> str:
        return (f"{self.action:4} {self.symbol:6} x{self.quantity:>6,}  "
                f"@~{self.ref_price:>8,.2f}  = ${self.notional:>10,.0f}"
                + (f"   ({self.reason})" if self.reason else ""))


def weights_to_orders(
    target_weights: dict[str, float],
    current_positions: dict[str, float],
    prices: dict[str, float],
    nav: float,
    min_trade_value: float = 200.0,
    drift_threshold: float = 0.01,
) -> list[OrderPlan]:
    """
    目标权重 -> 订单列表。这是回测世界和真实世界的接缝。

    两个过滤器决定了实盘和回测的差距有多大：
      drift_threshold  偏离多少才动手。设 0 就是每天全额调仓，手续费会吃掉一切。
      min_trade_value  小于这个金额不值得交易。
    """
    plans: list[OrderPlan] = []
    all_symbols = set(target_weights) | set(current_positions)

    for sym in sorted(all_symbols):
        price = prices.get(sym)
        if not price or price != price or price <= 0:
            log.warning("%s 无有效价格，跳过（宁可不做，也不要按错价格做）", sym)
            continue

        target_w = target_weights.get(sym, 0.0)
        cur_shares = current_positions.get(sym, 0.0)
        cur_w = cur_shares * price / nav if nav else 0.0
        drift = cur_w - target_w

        if abs(drift) < drift_threshold:
            continue

        delta_value = (target_w - cur_w) * nav
        if abs(delta_value) < min_trade_value:
            continue

        qty = int(abs(delta_value) / price)  # 截断而非四舍五入，避免超买
        if qty == 0:
            continue

        plans.append(OrderPlan(
            symbol=sym,
            action="BUY" if delta_value > 0 else "SELL",
            quantity=qty,
            ref_price=price,
            reason=f"{cur_w:.1%} -> {target_w:.1%}",
        ))

    # 先卖后买：保证买入时现金已经到位（现金账户尤其重要）
    plans.sort(key=lambda p: 0 if p.action == "SELL" else 1)
    return plans


def _preflight(plans: list[OrderPlan], nav: float) -> None:
    """执行前的体检。任何一项不过就整体拒绝，而不是跳过单笔。"""
    for p in plans:
        if p.quantity <= 0:
            raise ValueError(f"非法数量: {p}")
        if p.ref_price <= 0 or p.ref_price != p.ref_price:
            raise ValueError(f"非法价格: {p}")
        if MAX_ORDER_VALUE is not None and p.notional > MAX_ORDER_VALUE:
            raise ValueError(
                f"单笔金额 ${p.notional:,.0f} 超过绝对上限 ${MAX_ORDER_VALUE:,.0f}: {p}\n"
                f"确认无误的话，去 ibkr/execution.py 调 MAX_ORDER_VALUE。"
            )

    if not nav:
        log.warning("没有传入 nav，跳过按净值比例的熔断检查。这不是你想要的。")
        return

    for p in plans:
        if p.notional / nav > MAX_ORDER_PCT_NAV:
            raise ValueError(
                f"单笔 ${p.notional:,.0f} = 净值的 {p.notional/nav:.0%}，"
                f"超过上限 {MAX_ORDER_PCT_NAV:.0%}: {p}\n"
                f"这个量级通常意味着数量或权重算错了，先去查上游。"
            )

    turnover = sum(p.notional for p in plans)
    if turnover / nav > MAX_TOTAL_TURNOVER:
        raise ValueError(
            f"本次总换手 ${turnover:,.0f} = 净值的 {turnover/nav:.0%}，"
            f"超过上限 {MAX_TOTAL_TURNOVER:.0%}。先检查是不是信号算错了。"
        )


def _build_order(plan: OrderPlan, order_type: str, limit_band_bps: float) -> Order:
    if order_type == "MKT":
        return MarketOrder(plan.action, plan.quantity)
    # 可成交限价单：比市价单多一层保护，防止流动性瞬间消失时成交在离谱价位
    band = limit_band_bps / 10_000
    px = plan.ref_price * (1 + band) if plan.action == "BUY" else plan.ref_price * (1 - band)
    return LimitOrder(plan.action, plan.quantity, round(px, 2))


def execute_plan(
    ib: IB,
    plans: list[OrderPlan],
    nav: float = 0.0,
    dry_run: bool = True,
    what_if: bool = False,
    order_type: str = "LMT",
    limit_band_bps: float = 20.0,
    wait_seconds: float = 60.0,
) -> pd.DataFrame:
    """
    执行订单计划。

    dry_run=True   只打印（默认）
    what_if=True   发给 IBKR 做保证金预演，不会成交 —— 上实盘前的最后一次彩排
    order_type     'LMT'（默认，可成交限价）或 'MKT'
    """
    if not plans:
        log.info("没有需要执行的订单。")
        return pd.DataFrame()

    _preflight(plans, nav)

    # 注意：logging 用的是 %-style，不支持 %,.0f 那种千分位写法
    # （那是 str.format / f-string 的语法）。要千分位就先格式化成字符串再传。
    log.info("订单计划（共 %d 笔，名义金额 $%s）:",
             len(plans), f"{sum(p.notional for p in plans):,.0f}")
    for p in plans:
        log.info("  %s", p)

    if dry_run and not what_if:
        log.warning("DRY_RUN=True —— 以上订单均未发送。确认无误后传 dry_run=False。")
        return pd.DataFrame([p.__dict__ for p in plans])

    # 注意：ib_async 的 readonly 连接参数不会阻止 placeOrder，
    # 所以这一道拦截必须我们自己做（真正的强制在 TWS 的 "Read-Only API" 开关）。
    if not what_if and getattr(ib, "readonly_mode", False):
        raise RuntimeError("当前是只读连接，拒绝下单。去 .env 把 IB_READONLY 设为 false。")

    results = []
    for p in plans:
        contract = make_contract(p.symbol)
        ib.qualifyContracts(contract)
        order = _build_order(p, order_type, limit_band_bps)
        order.whatIf = what_if
        if config.ACCOUNT:
            order.account = config.ACCOUNT

        trade = ib.placeOrder(contract, order)

        if what_if:
            ib.sleep(2)
            st = trade.orderStatus
            log.info("  [whatIf] %s %s x%d -> 初始保证金 %s / 维持保证金 %s / 佣金 %s",
                     p.action, p.symbol, p.quantity,
                     st.initMarginChange, st.maintMarginChange, st.commission)
            results.append({**p.__dict__, "status": "whatIf",
                            "init_margin": st.initMarginChange,
                            "commission": st.commission})
            continue

        # 等终态。不做 fire-and-forget —— 你必须知道到底成没成。
        waited = 0.0
        while not trade.isDone() and waited < wait_seconds:
            ib.waitOnUpdate(timeout=1)
            waited += 1

        st = trade.orderStatus
        if not trade.isDone():
            log.warning("  %s %s 超时未终结，状态=%s 已成交=%s。订单仍在 IBKR 挂着，"
                        "去 TWS 里确认。", p.action, p.symbol, st.status, st.filled)
        else:
            log.info("  %s %s -> %s 成交 %s 股 @ %s",
                     p.action, p.symbol, st.status, st.filled, st.avgFillPrice)

        results.append({**p.__dict__, "status": st.status,
                        "filled": st.filled, "avg_fill_price": st.avgFillPrice})

    return pd.DataFrame(results)


def cancel_all(ib: IB) -> int:
    """紧急止血：撤掉所有未成交订单。"""
    trades = ib.openTrades()
    for t in trades:
        ib.cancelOrder(t.order)
    log.warning("已发出撤单请求 %d 笔。", len(trades))
    return len(trades)
