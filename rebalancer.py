"""
IBKR 组合再平衡器 —— 入门骨架
================================
读取 Paper 账户持仓 -> 对比目标权重 -> 计算偏离 -> 生成订单

设计要点（这些才是重点，不是策略本身）：
  1. 默认 DRY_RUN，绝不会误下单
  2. 显式校验连接的是 Paper 端口，防止手滑连实盘
  3. 所有决策过程打日志，事后可复盘
  4. 下单后跟踪订单状态，不是 fire-and-forget

依赖: pip install ib_async pandas
准备: 启动 IB Gateway (Paper)，Configure -> API -> Settings
      勾选 "Enable ActiveX and Socket Clients"
      勾选 "Download open orders on connection"
      Socket port = 4002
"""

import logging
from dataclasses import dataclass

from ib_async import IB, MarketOrder, Stock

# ---------------- 配置 ----------------
HOST = "127.0.0.1"
PORT = 4002          # Paper Gateway=4002, Paper TWS=7497, 实盘=4001/7496
CLIENT_ID = 11       # 每个独立程序用不同 clientId，否则会互相踢掉
ALLOW_LIVE = False   # 想连实盘时才改 True，是一道刻意的摩擦

TARGET_WEIGHTS = {
    "VTI": 0.40,   # 美股全市场
    "VXUS": 0.20,  # 美国以外
    "BND": 0.30,   # 债券
    "GLD": 0.10,   # 黄金
}

DRIFT_THRESHOLD = 0.03   # 绝对权重偏离超过 3 个百分点才动手
MIN_TRADE_VALUE = 200    # 低于此金额不值得付手续费
DRY_RUN = True           # 改成 False 才真正下单

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("rebalancer")


@dataclass
class Leg:
    symbol: str
    target_w: float
    current_w: float
    current_value: float
    target_value: float
    price: float

    @property
    def drift(self) -> float:
        return self.current_w - self.target_w

    @property
    def delta_value(self) -> float:
        return self.target_value - self.current_value

    @property
    def delta_shares(self) -> int:
        # 整股交易；截断而非四舍五入，避免超买
        return int(self.delta_value / self.price)


def connect() -> IB:
    if PORT in (4001, 7496) and not ALLOW_LIVE:
        raise RuntimeError(f"端口 {PORT} 是实盘端口，但 ALLOW_LIVE=False。已阻止连接。")
    ib = IB()
    ib.connect(HOST, PORT, clientId=CLIENT_ID, timeout=15)
    log.info("已连接 %s:%s (clientId=%s)", HOST, PORT, CLIENT_ID)
    return ib


def net_liquidation(ib: IB) -> float:
    """账户净清算价值 —— 分母，一切权重的基准。"""
    for row in ib.accountSummary():
        if row.tag == "NetLiquidation" and row.currency == "USD":
            return float(row.value)
    raise RuntimeError("未取到 NetLiquidation")


def build_legs(ib: IB, nav: float) -> list[Leg]:
    contracts = [Stock(s, "SMART", "USD") for s in TARGET_WEIGHTS]
    ib.qualifyContracts(*contracts)

    # 一次性拉快照价格；无实时订阅时会退化成延迟价，够用于研究
    tickers = ib.reqTickers(*contracts)
    prices = {t.contract.symbol: t.marketPrice() for t in tickers}

    held = {p.contract.symbol: p.position for p in ib.positions()}

    legs = []
    for symbol, target_w in TARGET_WEIGHTS.items():
        price = prices.get(symbol)
        if not price or price != price:  # None 或 NaN
            raise RuntimeError(f"{symbol} 无有效价格，中止（宁可不做也不要按错价格做）")
        shares = held.get(symbol, 0)
        value = shares * price
        legs.append(
            Leg(
                symbol=symbol,
                target_w=target_w,
                current_w=value / nav,
                current_value=value,
                target_value=nav * target_w,
                price=price,
            )
        )
    return legs


def decide(legs: list[Leg]) -> list[tuple[Leg, str, int]]:
    """返回 (leg, action, qty) 列表。先卖后买，保证现金充足。"""
    orders = []
    for leg in legs:
        # logging 是 %-style，不支持 %,.0f 千分位（那是 f-string 语法），
        # 直接写会抛 ValueError。先格式化成字符串再传。
        log.info(
            "%-5s 目标 %5.1f%% | 当前 %5.1f%% | 偏离 %+5.1f pp | 需调整 $%s",
            leg.symbol, leg.target_w * 100, leg.current_w * 100,
            leg.drift * 100, f"{leg.delta_value:+,.0f}",
        )
        if abs(leg.drift) < DRIFT_THRESHOLD:
            continue
        if abs(leg.delta_value) < MIN_TRADE_VALUE:
            continue
        qty = abs(leg.delta_shares)
        if qty == 0:
            continue
        orders.append((leg, "BUY" if leg.delta_value > 0 else "SELL", qty))

    orders.sort(key=lambda x: 0 if x[1] == "SELL" else 1)
    return orders


def execute(ib: IB, orders):
    if not orders:
        log.info("无需再平衡。")
        return

    for leg, action, qty in orders:
        log.info("拟下单: %s %s x%d  (~$%s)", action, leg.symbol, qty,
                 f"{qty * leg.price:,.0f}")
        if DRY_RUN:
            continue

        contract = Stock(leg.symbol, "SMART", "USD")
        ib.qualifyContracts(contract)
        trade = ib.placeOrder(contract, MarketOrder(action, qty))

        # 等待终态，不做 fire-and-forget
        while not trade.isDone():
            ib.waitOnUpdate(timeout=1)
        log.info(
            "  -> %s 成交 %s 股 @ %s",
            trade.orderStatus.status, trade.orderStatus.filled,
            trade.orderStatus.avgFillPrice,
        )

    if DRY_RUN:
        log.warning("DRY_RUN=True，以上订单均未发送。")


def main():
    ib = connect()
    try:
        nav = net_liquidation(ib)
        log.info("账户净值: $%s", f"{nav:,.2f}")
        legs = build_legs(ib, nav)
        execute(ib, decide(legs))
    finally:
        ib.disconnect()
        log.info("已断开连接。")


if __name__ == "__main__":
    main()