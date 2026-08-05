"""
候选资格审查。

agent 可以自由提名任何美股代码 —— 这是"自主选股"的意思。但提名不等于可交易，
每个代码都要过这里的门槛才进候选池，而候选池是 policy.py 的白名单来源。

四道门，每一道都对应一种真实的亏钱方式：

  流动性   agent 挑了个日成交额 200 万的小票，你按净值 30% 去买。
           冲击成本能吃掉几个点，而且卖的时候更糟。这是最容易被忽略的一种亏损，
           因为它不体现在任何一个你会去看的数字上。
  价格     低价股的最小变动价位占比大，且往往伴随退市风险。
  历史长度 没有两年数据就算不出 200 日均线和 52 周高低点，
           datapack 会给出一堆 None，agent 拿着一半是空的表做决策。
  杠杆/反向 TQQQ、SQQQ、UVXY 这类产品流动性极好，前三道门全都拦不住。
           但它们有日内复利衰减，按"持有几天到几周"的思路拿是纯粹的负期望。
           这道门必须靠名单和名称关键词。

设计上都是**拒绝比放过更安全**：拿不到数据、认不出合约、查不到名称 —— 一律不通过。
"""

from __future__ import annotations

import logging
import re

import pandas as pd
from ib_async import IB

import config
from ibkr.market_data import download_bars, make_contract

log = logging.getLogger("agent.screen")

# 杠杆 / 反向 / 波动率产品。它们的共同点是流动性筛不掉，只能点名。
# 这份名单不可能穷尽（每个月都有新的单股杠杆 ETF 上市），所以后面还有
# 一道按合约全称关键词的自动过滤，两道一起用。
DENY_SYMBOLS = {
    # 宽基杠杆 / 反向
    "TQQQ", "SQQQ", "QLD", "PSQ", "QID",
    "UPRO", "SPXU", "SSO", "SDS", "SH", "SPXS", "SPXL",
    "UDOW", "SDOW", "DDM", "DXD", "TNA", "TZA", "URTY", "SRTY",
    # 行业 / 主题杠杆
    "SOXL", "SOXS", "LABU", "LABD", "FAS", "FAZ", "TECL", "TECS",
    "NUGT", "DUST", "JNUG", "JDST", "ERX", "ERY", "YINN", "YANG",
    "BOIL", "KOLD", "UCO", "SCO", "AGQ", "ZSL", "UGL", "GLL",
    "TMF", "TMV", "TYD", "TYO", "UBT", "TBT", "TBF",
    # 波动率
    "UVXY", "SVXY", "VXX", "VIXY", "VIXM", "TVIX", "UVIX", "SVIX",
    # 单股杠杆（挂一漏万，靠下面的名称过滤兜底）
    "TSLL", "TSLQ", "TSLS", "NVDL", "NVDX", "NVDS", "AAPU", "AAPD",
    "MSFU", "MSFD", "AMZU", "AMZD", "GGLL", "GGLS", "METU", "METD",
    "CONL", "MSTU", "MSTX", "MSTZ", "AMDL", "AMUU",
}

# 合约全称里出现这些词，基本可以断定是杠杆 / 反向 / 波动率产品。
# 之所以用全称而不是代码：新产品的代码你不认识，但 IBKR 给的 longName 里
# 一定会写 "2X" "Daily Inverse" 之类的字样 —— 这是监管要求的。
#
# 必须用词边界而不是子串匹配。"BEAR" 用子串会打中 "BEARING"（轴承公司），
# "ULTRA" 会打中 "ULTRAGENYX"（一家生物制药公司）—— 把正常公司误判成杠杆产品，
# 表现是 agent 提名它却总被拒，而拒绝理由看起来完全合理，你不会去查。
DENY_NAME_PATTERNS = (
    r"\b\d+(\.\d+)?X\b",                     # 2X / 3X / 1.5X / -1X 里的 1X
    # 后缀要一个个列，不能用 \bULTRA\w*\b —— 那会打中 ULTRAGENYX（生物制药）
    r"\bULTRA(PRO|SHORT|PROSHORT)?\b",
    r"\bLEVERAGED\b",
    r"\bINVERSE\b",
    r"\bBEAR\b",
    r"\bDAILY\s+(SHORT|INVERSE|BEAR)\b",
    r"\b(SHORT|BEAR)\s+DAILY\b",
    r"\bVIX\b",
    r"\bVOLATILITY\s+(INDEX|FUTURES)\b",
)

_DENY_NAME_RE = re.compile("|".join(DENY_NAME_PATTERNS))


def _name_looks_leveraged(name: str) -> str | None:
    """返回命中的片段；没命中返回 None。"""
    m = _DENY_NAME_RE.search(name.upper())
    return m.group(0) if m else None


def screen_symbol(ib: IB, symbol: str,
                  bars: pd.DataFrame | None = None) -> tuple[bool, str, pd.DataFrame | None]:
    """
    审一个代码。返回 (是否通过, 原因, 日线数据)。

    通过时原因里写的是通过的依据（成交额、历史长度），因为这些数字
    agent 也需要看到 —— 它得知道自己提名的票为什么被留下或被拒绝，
    否则下次还会提名一样的东西。

    bars 已经在外面下好了就传进来。IBKR 历史数据 10 分钟只有约 60 次额度，
    同一个标的下两遍等于白扔掉一半 —— 而额度用光的表现是**静默拒绝**
    （错误 162），你会以为数据下好了，其实缺了一块。
    """
    sym = symbol.strip().upper()

    if not sym or not sym.replace(".", "").replace("-", "").isalnum():
        return False, f"{sym}: 不像一个合法的股票代码", None
    if sym in DENY_SYMBOLS:
        return False, f"{sym}: 在杠杆/反向/波动率产品黑名单里", None
    if sym in config.AGENT_DENY:
        return False, f"{sym}: 在 .env 的 AGENT_DENY 里被你手动拉黑了", None

    # 合约必须是美股普通股/ETF。期权、期货、外汇、非美元品种一律不做 ——
    # 下游 execution 层是按整股 + SMART 路由写的，别的品种它处理不了。
    contract = make_contract(sym)
    try:
        details = ib.reqContractDetails(contract)
    except Exception as e:  # noqa: BLE001
        return False, f"{sym}: 查合约出错（{e}）", None
    if not details:
        return False, f"{sym}: IBKR 认不出这个代码", None

    d = details[0]
    if d.contract.secType != "STK" or d.contract.currency != "USD":
        return False, f"{sym}: 不是美元普通股/ETF（secType={d.contract.secType}，" \
                      f"currency={d.contract.currency}）", None

    # 名称关键词只对基金类产品生效。
    #
    # 普通股不必查，而且查了会出错：ULTRA CLEAN HOLDINGS（半导体设备，UCTT）
    # 会被 \bULTRA\b 打中，TIMKEN 那类轴承公司会被 \bBEAR\b 打中。
    # 误判的表现很隐蔽 —— agent 提名它、被拒、拒绝理由看起来完全合理，
    # 于是这只票被永久地、静悄悄地排除在外，而你不会去查。
    #
    # stockType 取不到时（空字符串）按基金处理，宁可误杀不可放过。
    stock_type = (getattr(d, "stockType", "") or "").upper()
    long_name = d.longName or ""
    if stock_type not in ("COMMON", "ADR", "REIT", "PREFERRED"):
        hit = _name_looks_leveraged(long_name)
        if hit:
            return False, (f"{sym}: 全称含 '{hit}' —— 看起来是杠杆/反向/波动率产品"
                           f"（{long_name}，stockType={stock_type or '未知'}）"), None

    if bars is None:
        try:
            bars = download_bars(ib, sym, duration="3 Y", bar_size="1 day",
                                 what_to_show="ADJUSTED_LAST")
        except Exception as e:  # noqa: BLE001
            return False, f"{sym}: 取不到历史数据（{e}）", None
    if bars is None or bars.empty:
        return False, f"{sym}: 没有可用的日线数据", None

    if len(bars) < config.AGENT_MIN_HISTORY:
        return False, f"{sym}: 只有 {len(bars)} 根日线，少于要求的 " \
                      f"{config.AGENT_MIN_HISTORY} 根", None

    last_close = float(bars["close"].iloc[-1])
    if last_close < config.AGENT_MIN_PRICE:
        return False, f"{sym}: 最新价 ${last_close:.2f} 低于下限 " \
                      f"${config.AGENT_MIN_PRICE:.2f}", None

    # 用中位数而不是均值：一天的异常放量（财报、指数调整）能把均值拉高好几倍，
    # 让一只平时根本没量的票混进来。中位数对这种尖峰免疫。
    recent = bars.tail(60)
    dollar_vol = float((recent["close"] * recent["volume"]).median())
    if dollar_vol < config.AGENT_MIN_DOLLAR_VOL:
        return False, f"{sym}: 60日中位成交额 ${dollar_vol/1e6:.1f}M 低于下限 " \
                      f"${config.AGENT_MIN_DOLLAR_VOL/1e6:.0f}M", None

    return True, f"{sym}: 通过（${last_close:.2f}，60日中位成交额 " \
                 f"${dollar_vol/1e6:.0f}M，{len(bars)} 根日线）", bars


def screen_many(ib: IB, symbols: list[str]) -> tuple[dict[str, pd.DataFrame], list[str]]:
    """
    批量审查。返回 ({通过的代码: 日线}, [每个代码的结论文本])。

    一个不通过不影响其他的 —— agent 提名 20 个被刷掉 3 个是正常情况，
    没必要整批重来。但结论要全部回给它看。
    """
    passed: dict[str, pd.DataFrame] = {}
    notes: list[str] = []
    for sym in symbols:
        ok, why, bars = screen_symbol(ib, sym)
        notes.append(("  [通过] " if ok else "  [拒绝] ") + why)
        log.info("%s %s", "通过" if ok else "拒绝", why)
        if ok:
            passed[sym.strip().upper()] = bars
    return passed, notes
