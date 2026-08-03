"""
账户视图：净值、持仓、盈亏。

关键概念（新手最容易搞混的几个数）：
  NetLiquidation   账户净清算值 = 现金 + 持仓市值。所有权重的分母，看这个。
  TotalCashValue   现金
  GrossPositionValue 持仓市值绝对值之和（多空都算正）
  BuyingPower      购买力。融资账户 ≈ 净值 x 杠杆倍数，别把它当成"我有这么多钱"
  UnrealizedPnL    浮动盈亏
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import pandas as pd
from ib_async import IB

import config

log = logging.getLogger("ibkr.account")

SUMMARY_TAGS = [
    "NetLiquidation",
    "TotalCashValue",
    "GrossPositionValue",
    "BuyingPower",
    "AvailableFunds",
    "UnrealizedPnL",
    "RealizedPnL",
]


def account_summary(ib: IB, account: str | None = None) -> dict[str, float]:
    """返回 {tag: value}，只取 USD（或 BASE）口径。"""
    acct = account or config.ACCOUNT or ""
    rows = ib.accountSummary(acct) if acct else ib.accountSummary()

    out: dict[str, float] = {}
    for r in rows:
        if r.tag not in SUMMARY_TAGS:
            continue
        if r.currency not in ("USD", "BASE", ""):
            continue
        try:
            out[r.tag] = float(r.value)
        except ValueError:
            continue

    if "NetLiquidation" not in out:
        raise RuntimeError(
            "取不到 NetLiquidation。账户可能还没初始化完，"
            "或者 IB_ACCOUNT 填错了。ib.managedAccounts() 可以看有哪些账户。"
        )
    return out


def positions_df(ib: IB, account: str | None = None) -> pd.DataFrame:
    """
    当前持仓表。列：symbol, secType, currency, position, avg_cost,
                  market_price, market_value, unrealized_pnl, weight
    空仓时返回一个列齐全的空表 —— 下游代码不用为空表写特例。
    """
    cols = ["symbol", "secType", "currency", "position", "avg_cost",
            "market_price", "market_value", "unrealized_pnl", "weight"]

    acct = account or config.ACCOUNT
    items = [p for p in ib.portfolio() if not acct or p.account == acct]

    rows = []
    for p in items:
        rows.append({
            "symbol": p.contract.symbol,
            "secType": p.contract.secType,
            "currency": p.contract.currency,
            "position": p.position,
            "avg_cost": p.averageCost,
            "market_price": p.marketPrice,
            "market_value": p.marketValue,
            "unrealized_pnl": p.unrealizedPNL,
        })

    if not rows:
        # portfolio() 依赖 reqAccountUpdates，多账户时可能是空的，退回 positions()
        for p in ib.positions(acct or ""):
            rows.append({
                "symbol": p.contract.symbol,
                "secType": p.contract.secType,
                "currency": p.contract.currency,
                "position": p.position,
                "avg_cost": p.avgCost,
                "market_price": float("nan"),
                "market_value": float("nan"),
                "unrealized_pnl": float("nan"),
            })

    if not rows:
        return pd.DataFrame(columns=cols)

    df = pd.DataFrame(rows)
    total = df["market_value"].abs().sum()
    df["weight"] = df["market_value"] / total if total else 0.0
    return df.sort_values("market_value", ascending=False, ignore_index=True)[cols]


def open_orders_df(ib: IB) -> pd.DataFrame:
    cols = ["orderId", "symbol", "action", "orderType", "quantity",
            "lmtPrice", "status", "filled", "remaining"]
    rows = []
    for t in ib.openTrades():
        rows.append({
            "orderId": t.order.orderId,
            "symbol": t.contract.symbol,
            "action": t.order.action,
            "orderType": t.order.orderType,
            "quantity": t.order.totalQuantity,
            "lmtPrice": getattr(t.order, "lmtPrice", None),
            "status": t.orderStatus.status,
            "filled": t.orderStatus.filled,
            "remaining": t.orderStatus.remaining,
        })
    return pd.DataFrame(rows, columns=cols)


def fills_df(ib: IB) -> pd.DataFrame:
    """当日成交。注意：IBKR 的 API 只给当天的，历史成交要去 Flex Query 拉。"""
    cols = ["time", "symbol", "side", "shares", "price", "commission", "realized_pnl"]
    rows = []
    for f in ib.fills():
        rows.append({
            "time": f.execution.time,
            "symbol": f.contract.symbol,
            "side": f.execution.side,
            "shares": f.execution.shares,
            "price": f.execution.price,
            "commission": getattr(f.commissionReport, "commission", None),
            "realized_pnl": getattr(f.commissionReport, "realizedPNL", None),
        })
    return pd.DataFrame(rows, columns=cols)


# ---------------- 快照：解耦 dashboard 和实时连接 ----------------
# dashboard 不该自己持有 broker 连接。让脚本定期把状态写到磁盘，
# dashboard 只读文件 —— 这样 dashboard 崩了不影响交易，交易断了也能看历史。

def save_snapshot(ib: IB, account: str | None = None) -> dict:
    summary = account_summary(ib, account)
    pos = positions_df(ib, account)
    ts = datetime.now(timezone.utc)

    snap = {
        "timestamp": ts.isoformat(),
        "port": config.PORT,
        "env": "live" if config.is_live_port() else "paper",
        "summary": summary,
        "positions": pos.to_dict("records"),
    }

    (config.SNAPSHOT_DIR / "latest.json").write_text(
        json.dumps(snap, indent=2, default=str), encoding="utf-8"
    )
    # 同时按日追加一条净值记录，攒成自己的净值曲线
    equity_path = config.SNAPSHOT_DIR / "equity_curve.csv"
    line = pd.DataFrame([{
        "timestamp": ts.isoformat(),
        "net_liquidation": summary["NetLiquidation"],
        "cash": summary.get("TotalCashValue"),
        "unrealized_pnl": summary.get("UnrealizedPnL"),
    }])
    line.to_csv(equity_path, mode="a", header=not equity_path.exists(), index=False)

    log.info("快照已写入 %s", config.SNAPSHOT_DIR)
    return snap


def load_snapshot() -> dict | None:
    p = config.SNAPSHOT_DIR / "latest.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def load_equity_curve() -> pd.DataFrame:
    p = config.SNAPSHOT_DIR / "equity_curve.csv"
    if not p.exists():
        return pd.DataFrame(columns=["timestamp", "net_liquidation", "cash", "unrealized_pnl"])
    df = pd.read_csv(p, parse_dates=["timestamp"])
    return df.sort_values("timestamp", ignore_index=True)
