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

    # 先把所有合约认全，再发第一笔单。
    #
    # 注意 qualifyContracts 的返回语义很容易看错：它返回的列表**长度永远等于入参个数**，
    # 认不出来的位置放 None（见 ib_async 的 qualifyContractsAsync 文档）。
    # 所以 `if not ib.qualifyContracts(c)` 判断的是 `not [None]` == False，永远不触发。
    # 必须逐个查 None / conId。
    #
    # 为什么要一次性全查完再下单：认不出来的合约是个 conId=0 的空壳，拿它 placeOrder
    # 行为未定义。而如果放在循环里逐个查，失败发生在中途 —— 前几笔已经成交，
    # 组合停在一个谁也没想要的中间状态。要么全做，要么一笔都不做。
    contracts = [make_contract(p.symbol) for p in plans]
    qualified = ib.qualifyContracts(*contracts)
    unknown = [p.symbol for p, c in zip(plans, qualified)
               if c is None or not getattr(c, "conId", 0)]
    if unknown:
        raise ValueError(
            f"这些代码 IBKR 认不出来: {unknown}\n"
            f"常见原因：拼写错误，或者标的池里混进了 00_offline_demo.py 生成的 "
            f"SYNTH_* 合成数据。一笔都不会发送。"
        )

    results = []
    for p, contract in zip(plans, qualified):
        order = _build_order(p, order_type, limit_band_bps)
        if config.ACCOUNT:
            order.account = config.ACCOUNT

        if what_if:
            # 保证金预演必须走 whatIfOrder。
            # 不能用 placeOrder(whatIf=True) 再读 trade.orderStatus —— OrderStatus 上
            # 根本没有 initMarginChange / commission，它们在 OrderState 上，
            # 而 Trade 不携带 OrderState。读了就是 AttributeError。
            #
            # whatIfOrder 标注返回 OrderState，但只有当 IBKR 回的 orderState 里
            # initMarginChange 有值时 ib_async 才会兑现那个 future；否则超时后
            # 返回内部默认值 —— 一个空列表。所以返回类型实际上是不确定的。
            st = ib.whatIfOrder(contract, order)
            if isinstance(st, list):
                st = st[0] if st else None

            if st is None or not hasattr(st, "initMarginChange"):
                # 这一步什么也没校验成。必须说清楚，否则"跑完了没报错"会被当成通过。
                log.warning("  [whatIf] %s 没拿到保证金回执，这笔没有被校验。", p.symbol)
                results.append({**p.__dict__, "status": "whatIf-无回执"})
                continue

            log.info("  [whatIf] %s %s x%d -> 初始保证金 %s / 维持保证金 %s / 佣金 %s",
                     p.action, p.symbol, p.quantity,
                     st.initMarginChange, st.maintMarginChange, st.commission)
            if st.warningText:
                log.warning("  [whatIf] %s IBKR 警告: %s", p.symbol, st.warningText)
            results.append({**p.__dict__, "status": "whatIf",
                            "init_margin": st.initMarginChange,
                            "maint_margin": st.maintMarginChange,
                            "commission": st.commission,
                            "warning": st.warningText})
            continue

        trade = ib.placeOrder(contract, order)

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

        results.append({**p.__dict__, "order_id": trade.order.orderId,
                        "status": st.status,
                        "filled": st.filled, "avg_fill_price": st.avgFillPrice})

    # 以 IBKR 为准复核一遍。本地状态会被"假取消"污染（见 cancel_all 的注释），
    # 一笔活着的单可能刚被我们报成 Cancelled —— 报错了比没报更危险，
    # 因为后面的对账会以为仓位已经落定。
    if not what_if and results:
        ib.sleep(1)
        live = {t.order.orderId: t.orderStatus.status for t in ib.reqAllOpenOrders()}
        for row in results:
            oid = row.get("order_id")
            if oid in live and row["status"] not in ("Filled",):
                if row["status"] != live[oid]:
                    log.warning("  %s 本地状态是 %s，但 IBKR 侧仍在挂单（%s）—— 以 IBKR 为准。",
                                row["symbol"], row["status"], live[oid])
                row["status"] = live[oid]
                row["still_open"] = True

    df = pd.DataFrame(results)

    # 一笔都没拿到回执，说明 --what-if 这一步整个是空转。
    # 不吭声的话，"跑完了没报错"会被当成"IBKR 校验通过了"，那比不做还危险。
    if what_if and not df.empty and (df["status"] == "whatIf-无回执").all():
        log.warning(
            "所有 whatIf 请求都没拿到保证金回执 —— 这一步没有校验任何东西。\n"
            "  IBKR 只有在返回的 orderState 里带保证金数据时才会兑现请求，"
            "Paper Gateway 上经常不带。\n"
            "  别把它当成一道通过了的关卡；真正拦得住错误的是 _preflight 的熔断和 DRY_RUN。"
        )

    return df


def cancel_all(ib: IB, attempts: int = 3, settle: float = 4.0) -> int:
    """
    紧急止血：撤掉所有未成交订单。撤不干净就抛错。

    两个坑，都被踩过：

    1) 必须用 reqAllOpenOrders()（问 IBKR）而不是 openTrades()（读本地缓存）。
       ib_async 会因为某些纯提示性的券商消息把订单本地标成 Cancelled ——
       比如 10349 "Order TIF was set to DAY based on order preset"，它不在
       ib_async 的 warningCodes 白名单里，于是走进"这单出问题了，取消掉"的分支。
       此时订单在 IBKR 那边还好好活着，却已经从 openTrades() 里消失了。

    2) 必须循环到确认清空。刚发出去的单可能还没在券商侧登记完，
       单次快照会漏掉它 —— 实测就漏过一笔。

    撤漏一笔的后果不是"少撤了一笔"，是下一轮新单和它叠成超额仓位，
    而这正是调用方要防的事。所以宁可抛错中断，也不能报告一个假的成功。
    """
    total = 0
    for i in range(attempts):
        trades = ib.reqAllOpenOrders()
        if not trades:
            break
        for t in trades:
            ib.cancelOrder(t.order)
        total += len(trades)
        log.warning("第 %d 轮：发出撤单请求 %d 笔", i + 1, len(trades))
        ib.sleep(settle)

    left = ib.reqAllOpenOrders()
    if left:
        raise RuntimeError(
            f"撤了 {attempts} 轮，IBKR 侧仍有 {len(left)} 笔挂着："
            f"{[(t.contract.symbol, t.orderStatus.status) for t in left]}\n"
            f"去 TWS 里手动处理。继续下单会和它们叠成超额仓位。"
        )
    return total
