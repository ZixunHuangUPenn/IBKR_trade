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
AGENT_DIR = DATA_DIR / "agent"         # AI 自主选股作业的工作区

for _d in (BARS_DIR, SNAPSHOT_DIR, RESULTS_DIR, SIGNAL_DIR, LOGS_DIR, AGENT_DIR):
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


# ---------------- AI 自主选股作业（scripts/06_agent_signal.py）----------------
# 这一段和上面所有配置有一个本质区别：这里的每一条都是**硬上限**，不是给
# agent 的建议。prompt 里写的话 agent 可以不听，写在这里的它绕不过去。
#
# 违反任何一条的处理方式是"整份提案作废，当天不交易"，而不是"截断到上限后继续"。
# 截断出来的组合是 agent 从没考虑过的东西 —— 它可能本来打算 40% AAPL 配 60% 现金，
# 截断成 33% 之后变成 33% AAPL + 67% 现金，风险收益完全不是一回事。
# 不交易 = 保持昨天的仓位，这是唯一一个我们确定 agent 曾经认可过的状态。
AGENT_STRATEGY_NAME = "AI自主选股"

AGENT_MAX_WEIGHT = _float("AGENT_MAX_WEIGHT", 0.33)        # 单标的权重上限
AGENT_MAX_POSITIONS = _int("AGENT_MAX_POSITIONS", 6)       # 最多同时持有几只
AGENT_MAX_GROSS = _float("AGENT_MAX_GROSS", 1.00)          # 总仓位上限（1.0 = 不加杠杆）
# 单边换手上限 = Σ|Δw| / 2。0.5 的含义："一天最多动净值的一半"。
# 为什么用单边口径：空仓建满一个组合 Σ|Δw|=1.0，单边 0.5，刚好卡在上限内 ——
# 建仓日不会被误杀；而把 6 只全换成另外 6 只是单边 1.0，会被拦下。
# 换句话说，这条规则允许 agent 调整组合，但不允许它每天推倒重来。
AGENT_MAX_TURNOVER = _float("AGENT_MAX_TURNOVER", 0.50)

# 每天最多让 agent 拉多少个标的的数据。既是成本控制，也是 IBKR 限速保护
# （历史数据 10 分钟约 60 次，见 ibkr/market_data.py）。
AGENT_MAX_CANDIDATES = _int("AGENT_MAX_CANDIDATES", 30)

# 候选资格的硬门槛。agent 可以自由提名任何美股代码，但过不了这几关就不进候选池。
# 这几条挡的是"流动性陷阱"：agent 挑了个日成交额 200 万的小票，
# 你按净值 30% 去买，冲击成本能吃掉几个点，而回测里永远看不到这件事。
AGENT_MIN_PRICE = _float("AGENT_MIN_PRICE", 10.0)
AGENT_MIN_DOLLAR_VOL = _float("AGENT_MIN_DOLLAR_VOL", 50_000_000)  # 60日中位成交额
AGENT_MIN_HISTORY = _int("AGENT_MIN_HISTORY", 500)                 # 至少多少根日线

# 净值从历史高点回撤超过它就停止交易并告警。
# 这是没有回测的策略唯一能有的"止损" —— 你不知道它什么时候会失效，
# 只能规定亏到什么程度就必须停下来人工看一眼。
AGENT_MAX_DRAWDOWN = _float("AGENT_MAX_DRAWDOWN", 0.20)

# 额外拉黑的代码，逗号分隔。杠杆/反向 ETF 的内置名单见 agent/screen.py。
AGENT_DENY = [s.strip().upper() for s in os.getenv("AGENT_DENY", "").split(",") if s.strip()]

# 喂给 agent 的历史决策条数。它需要看见自己昨天为什么买，否则每天从零开始，
# 结果就是无意义的高换手 —— 这是 LLM 做投资决策最典型的失败模式。
AGENT_JOURNAL_DAYS = _int("AGENT_JOURNAL_DAYS", 5)

# agent 运行期间的进程级封条，由 run_agent.ps1 在调用 claude 之前设上。
# 设上之后，那一格里派生出来的任何 Python 进程都建不成"可下单"的连接
# （强制点在 ibkr/connection.py）。
#
# 为什么不直接用 IB_READONLY：那个开关会被 05_daily_job.py 的 --execute 覆盖掉
# （readonly = config.READONLY and not args.execute），所以它拦不住一个决定
# 自己去跑 trade 阶段的 agent。这个封条不接受任何参数覆盖。
AGENT_SANDBOX = _bool("IBKR_AGENT_SANDBOX", False)


# ---------------- 回测默认参数 ----------------
INITIAL_CAPITAL = 100_000.0
COMMISSION_BPS = 1.0    # 单边手续费，万分之一 ≈ IBKR 分级定价的量级
SLIPPAGE_BPS = 2.0      # 单边滑点假设，宁可保守
TRADING_DAYS = 252


# ---------------- 默认标的池 ----------------
# 从宽基 ETF 起步：流动性好、数据干净、没有个股的财报跳空和退市偏差
DEFAULT_UNIVERSE = ["SPY", "QQQ", "IWM", "EFA", "EEM", "TLT", "IEF", "GLD", "DBC", "VNQ"]
