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
SIGNAL_DIR = DATA_DIR / "signals"      # 待执行信号 + 历史归档
LOGS_DIR = DATA_DIR / "logs"           # 日常作业日志

for _d in (BARS_DIR, SNAPSHOT_DIR, RESULTS_DIR, SIGNAL_DIR, LOGS_DIR):
    _d.mkdir(parents=True, exist_ok=True)


def _bool(key: str, default: bool = False) -> bool:
    return os.getenv(key, str(default)).strip().lower() in ("1", "true", "yes", "on")


# .env 里把某一项留空（`IB_PORT=`）是很自然的写法，但 os.getenv 返回的是 ""
# 而不是 None，默认值根本轮不上，int("") 直接崩在 import 期。
# 崩在这里还算幸运 —— 更坏的情况是配置被误读成别的值，而你不知道。
def _int(key: str, default: int) -> int:
    v = os.getenv(key, "").strip()
    return int(v) if v else default


def _float(key: str, default: float) -> float:
    v = os.getenv(key, "").strip()
    return float(v) if v else default


# ---------------- 连接 ----------------
HOST = os.getenv("IB_HOST", "").strip() or "127.0.0.1"
PORT = _int("IB_PORT", 4002)
CLIENT_ID = _int("IB_CLIENT_ID", 10)
ALLOW_LIVE = _bool("IB_ALLOW_LIVE", False)
READONLY = _bool("IB_READONLY", True)
ACCOUNT = os.getenv("IB_ACCOUNT", "").strip() or None

MARKET_DATA_TYPE = _int("IB_MARKET_DATA_TYPE", 3)

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


# ---------------- 无人值守作业 ----------------
# 定时作业必须用和交互式工具（dashboard / 手动脚本）不同的 clientId。
# 同一个 clientId 连两次会互相踢掉 —— 你在作业跑的那一刻刷新一下 dashboard，
# 当天的调仓就没了，而且是静默的。默认 +1 保证改了 IB_CLIENT_ID 也依然错开。
JOB_CLIENT_ID = _int("IB_JOB_CLIENT_ID", CLIENT_ID + 1)

# Gateway 每天会自动重启，重启期间 API 连不上。定时任务撞上这个窗口是常态，
# 所以"连不上就退出"是不可接受的 —— 必须重试，且重试间隔要盖过重启耗时。
CONNECT_RETRIES = _int("IB_CONNECT_RETRIES", 5)
CONNECT_RETRY_DELAY = _float("IB_CONNECT_RETRY_DELAY", 60)

# 信号最多能放多久还算数（日历日）。周末 + 一个假日 = 3 天，留到 4 天。
# 超过说明信号作业已经挂了好几天了，这时候按陈旧信号下单比不下单危险得多。
MAX_SIGNAL_AGE_DAYS = _int("JOB_MAX_SIGNAL_AGE_DAYS", 4)

# 对账容忍度：单标的实际权重与目标权重的绝对偏差超过它就报警
RECONCILE_TOLERANCE = _float("JOB_RECONCILE_TOLERANCE", 0.02)

# 出问题时往哪发通知。留空 = 只写日志。
# 任何接受 POST JSON 的 URL 都行（Bark / Slack / 企业微信 / 自建接口）。
NOTIFY_WEBHOOK = os.getenv("NOTIFY_WEBHOOK", "").strip() or None


# ---------------- 回测默认参数 ----------------
INITIAL_CAPITAL = 100_000.0
COMMISSION_BPS = 1.0    # 单边手续费，万分之一 ≈ IBKR 分级定价的量级
SLIPPAGE_BPS = 2.0      # 单边滑点假设，宁可保守
TRADING_DAYS = 252


# ---------------- 默认标的池 ----------------
# 从宽基 ETF 起步：流动性好、数据干净、没有个股的财报跳空和退市偏差
DEFAULT_UNIVERSE = ["SPY", "QQQ", "IWM", "EFA", "EEM", "TLT", "IEF", "GLD", "DBC", "VNQ"]
