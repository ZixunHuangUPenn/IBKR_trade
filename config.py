"""
全局配置 —— 所有模块的唯一真相来源。

设计原则：连实盘必须是一件"需要刻意为之"的事。
这里放了三道锁，任何一道没打开都连不上实盘端口。
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ---------------- 路径 ----------------
ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
BARS_DIR = DATA_DIR / "bars"          # 历史K线 parquet 缓存
SNAPSHOT_DIR = DATA_DIR / "snapshots"  # 账户快照，给 dashboard 读
RESULTS_DIR = DATA_DIR / "results"     # 回测结果

for _d in (BARS_DIR, SNAPSHOT_DIR, RESULTS_DIR):
    _d.mkdir(parents=True, exist_ok=True)


def _bool(key: str, default: bool = False) -> bool:
    return os.getenv(key, str(default)).strip().lower() in ("1", "true", "yes", "on")


# ---------------- 连接 ----------------
HOST = os.getenv("IB_HOST", "127.0.0.1")
PORT = int(os.getenv("IB_PORT", "4002"))
CLIENT_ID = int(os.getenv("IB_CLIENT_ID", "10"))
ALLOW_LIVE = _bool("IB_ALLOW_LIVE", False)
READONLY = _bool("IB_READONLY", True)
ACCOUNT = os.getenv("IB_ACCOUNT", "").strip() or None

MARKET_DATA_TYPE = int(os.getenv("IB_MARKET_DATA_TYPE", "3"))

LIVE_PORTS = {4001, 7496}
PAPER_PORTS = {4002, 7497}


def is_live_port(port: int = None) -> bool:
    return (port or PORT) in LIVE_PORTS


def assert_safe_to_connect(port: int = None) -> None:
    """连接前的守门人。实盘端口 + ALLOW_LIVE=false => 直接抛错。"""
    p = port or PORT
    if p in LIVE_PORTS and not ALLOW_LIVE:
        raise RuntimeError(
            f"端口 {p} 是实盘端口，但 IB_ALLOW_LIVE=false。已阻止连接。\n"
            f"确认你真的要连实盘，再去 .env 里改。"
        )
    if p not in LIVE_PORTS and p not in PAPER_PORTS:
        # 不认识的端口不拦，但要吭声
        print(f"[config] 警告：端口 {p} 既不是已知的 Paper 也不是实盘端口。")


# ---------------- 回测默认参数 ----------------
INITIAL_CAPITAL = 100_000.0
COMMISSION_BPS = 1.0    # 单边手续费，万分之一 ≈ IBKR 分级定价的量级
SLIPPAGE_BPS = 2.0      # 单边滑点假设，宁可保守
TRADING_DAYS = 252


# ---------------- 默认标的池 ----------------
# 从宽基 ETF 起步：流动性好、数据干净、没有个股的财报跳空和退市偏差
DEFAULT_UNIVERSE = ["SPY", "QQQ", "IWM", "EFA", "EEM", "TLT", "IEF", "GLD", "DBC", "VNQ"]
