"""
事实包：把行情算成 agent 能读的表。

这个模块存在的唯一理由是**不让 agent 自己算数**。

LLM 算数会错，而且错得很自信 —— "AAPL 站上 200 日均线了" 这种话它张口就来，
你没法从输出里看出这是算的还是编的。所以约定很硬：

    所有数字由这里算出来，写进 context.md。
    agent 只允许引用表里出现过的数字，不许自己推导新的。

同样重要的是**别给太多**。塞 60 个标的 × 20 个指标进去，agent 会淹死在里面，
表现还不如给 10 个标的 × 8 个指标。这里选的指标覆盖三件事，够了：

    它在哪   多周期收益、离均线多远、离 52 周高低点多远
    它多颠   年化波动率、ATR、最大回撤 —— 定仓位大小要用
    有没有人在乎  量比。放量的动才是动，缩量的动是噪声
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

import numpy as np
import pandas as pd

import config

log = logging.getLogger("agent.datapack")

BENCHMARKS = ["SPY", "QQQ", "IWM", "TLT"]   # 判断市场环境用，不参与选股

JOURNAL = config.AGENT_DIR / "journal.jsonl"


def _pct(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or b == 0 or not np.isfinite(a) or not np.isfinite(b):
        return None
    return (a / b - 1.0) * 100.0


def _ret(close: pd.Series, days: int) -> float | None:
    if len(close) <= days:
        return None
    return _pct(float(close.iloc[-1]), float(close.iloc[-1 - days]))


def _fmt(v: float | None, suffix: str = "", digits: int = 1) -> str:
    # 拿不到的数字必须显示成 "—" 而不是 0 或者省略这一列。
    # 显示成 0 会被 agent 当成"这个指标是 0"，那是彻底的误读。
    if v is None or not np.isfinite(v):
        return "—"
    return f"{v:+.{digits}f}{suffix}" if suffix == "%" else f"{v:.{digits}f}{suffix}"


def compute_stats(symbol: str, bars: pd.DataFrame,
                  bench_close: pd.Series | None = None) -> dict:
    """单个标的的事实。数据不够就填 None，绝不用近似值顶上。"""
    close = bars["close"].astype(float)
    high, low = bars["high"].astype(float), bars["low"].astype(float)
    vol = bars["volume"].astype(float)
    last = float(close.iloc[-1])

    def sma_gap(n: int) -> float | None:
        if len(close) < n:
            return None
        return _pct(last, float(close.tail(n).mean()))

    # 年化波动率。252 是交易日数，见 config.TRADING_DAYS
    ret = close.pct_change()
    vol20 = (float(ret.tail(20).std()) * np.sqrt(config.TRADING_DAYS) * 100
             if len(ret) > 20 else None)

    # ATR：比标准差多考虑了跳空，用来定"正常的一天能波动多少"更贴切
    prev = close.shift(1)
    tr = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    atr = float(tr.tail(14).mean()) / last * 100 if len(tr) > 14 else None

    win = close.tail(252)
    hi52, lo52 = float(win.max()), float(win.min())

    # 252 日最大回撤：agent 定仓位时需要知道这东西历史上能跌多深
    roll_max = win.cummax()
    mdd = float(((win / roll_max - 1.0).min())) * 100 if len(win) > 20 else None

    v5 = float(vol.tail(5).mean()) if len(vol) >= 5 else None
    v60 = float(vol.tail(60).mean()) if len(vol) >= 60 else None
    vratio = v5 / v60 if v5 and v60 else None

    # 相对基准的 63 日超额。个股涨 10% 而 SPY 涨 12%，那不是强，是弱。
    # 不给这个数，agent 会把 beta 当成 alpha —— 牛市里所有票看起来都很强。
    excess = None
    if bench_close is not None and len(bench_close) > 63 and len(close) > 63:
        r_sym, r_bmk = _ret(close, 63), _ret(bench_close, 63)
        if r_sym is not None and r_bmk is not None:
            excess = r_sym - r_bmk

    return {
        "symbol": symbol,
        "close": last,
        "last_bar": str(bars.index[-1].date()),
        "ret_1d": _ret(close, 1),
        "ret_5d": _ret(close, 5),
        "ret_21d": _ret(close, 21),
        "ret_63d": _ret(close, 63),
        "ret_252d": _ret(close, 252),
        "excess_63d": excess,
        "vs_sma20": sma_gap(20),
        "vs_sma50": sma_gap(50),
        "vs_sma200": sma_gap(200),
        "vol20": vol20,
        "atr14": atr,
        "from_52w_high": _pct(last, hi52),
        "from_52w_low": _pct(last, lo52),
        "mdd_252d": mdd,
        "vol_ratio": vratio,
        "dollar_vol_60d": float((close.tail(60) * vol.tail(60)).median()),
    }


def stats_table(rows: list[dict]) -> str:
    """Markdown 表。列顺序按"先看在哪、再看多颠、最后看有没有人在乎"排。"""
    if not rows:
        return "_（空）_\n"

    head = ("| 代码 | 收盘 | 1日 | 5日 | 21日 | 63日 | 252日 | 超额63日 | vs20日线 | "
            "vs50日线 | vs200日线 | 年化波动 | ATR | 距52周高 | 距52周低 | "
            "252日最大回撤 | 量比 |")
    sep = "|" + "---|" * 17
    lines = [head, sep]
    for r in sorted(rows, key=lambda x: -(x["ret_63d"] or -999)):
        lines.append("| " + " | ".join([
            r["symbol"],
            f"{r['close']:.2f}",
            _fmt(r["ret_1d"], "%"), _fmt(r["ret_5d"], "%"), _fmt(r["ret_21d"], "%"),
            _fmt(r["ret_63d"], "%"), _fmt(r["ret_252d"], "%"), _fmt(r["excess_63d"], "%"),
            _fmt(r["vs_sma20"], "%"), _fmt(r["vs_sma50"], "%"), _fmt(r["vs_sma200"], "%"),
            _fmt(r["vol20"], "%", 0), _fmt(r["atr14"], "%"),
            _fmt(r["from_52w_high"], "%"), _fmt(r["from_52w_low"], "%"),
            _fmt(r["mdd_252d"], "%", 0),
            _fmt(r["vol_ratio"], "x", 2),
        ]) + " |")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- 组合与环境

def portfolio_block(state: dict) -> str:
    """当前持仓。agent 必须先看清自己已经拿着什么，才谈得上"改成什么"。"""
    nav = state["nav"]
    lines = [
        f"- 账户净值 **${nav:,.0f}**，现金 ${state['cash']:,.0f}"
        f"（{state['cash']/nav:.1%}）",
        f"- 已投入仓位 **{state['gross']:.1%}**，共 {len(state['positions'])} 个持仓",
    ]
    dd = state.get("drawdown")
    if dd is not None:
        lines.append(f"- 净值距历史高点 **{dd:+.1%}**"
                     f"（熔断线 -{config.AGENT_MAX_DRAWDOWN:.0%}）")

    if not state["positions"]:
        lines.append("\n**当前空仓。**")
        return "\n".join(lines) + "\n"

    lines += ["", "| 代码 | 股数 | 现价 | 市值 | 当前权重 | 成本 | 浮动盈亏 |",
              "|---|---|---|---|---|---|---|"]
    for p in state["positions"]:
        pnl_pct = (p["market_price"] / p["avg_cost"] - 1) * 100 if p.get("avg_cost") else None
        lines.append(
            f"| {p['symbol']} | {p['position']:,.0f} | {p['market_price']:.2f} | "
            f"${p['market_value']:,.0f} | {p['weight']:.1%} | "
            f"{p.get('avg_cost') or float('nan'):.2f} | {_fmt(pnl_pct, '%')} |"
        )
    return "\n".join(lines) + "\n"


def regime_block(bench_stats: list[dict]) -> str:
    """市场环境。不是给 agent 择时用的，是让它知道"今天所有票都在跌"这件事。"""
    if not bench_stats:
        return "_（基准数据缺失）_\n"
    return stats_table(bench_stats)


# ---------------------------------------------------------------- 决策日志

def append_journal(entry: dict) -> None:
    with JOURNAL.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")


def recent_journal(days: int = None) -> list[dict]:
    days = days if days is not None else config.AGENT_JOURNAL_DAYS
    if not JOURNAL.exists():
        return []
    out = []
    for line in JOURNAL.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue          # 一行坏了不该让整个作业停下来
    return out[-days:]


def journal_block(entries: list[dict]) -> str:
    """
    历史决策。这一段是整个 context 里最容易被低估的部分。

    没有它，agent 每天都是第一次见到这个组合，于是每天都会重新想一遍
    "现在最该买什么" —— 结果是无意义的高换手，把收益全交给手续费和滑点。
    给它看见自己前几天写的买入理由，它才有可能说出"逻辑没变，继续拿着"。
    """
    if not entries:
        return "_（还没有历史决策，今天是第一天）_\n"

    lines = []
    for e in entries:
        w = e.get("weights", {})
        lines.append(f"**{e.get('signal_date', '?')}** —— 目标：" +
                     ("、".join(f"{k} {v:.0%}" for k, v in sorted(w.items(), key=lambda x: -x[1]))
                      or "空仓"))
        for sym, thesis in (e.get("theses") or {}).items():
            lines.append(f"  - {sym}：{thesis}")
        if e.get("risk_notes"):
            lines.append(f"  - 风险备注：{e['risk_notes']}")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------- 拼装

def build_context(state: dict, bench_stats: list[dict], held_stats: list[dict],
                  now_et: datetime, limits: str) -> str:
    """
    写 context.md 的主体。候选标的的表由 fetch 阶段追加在后面。

    limits 由 policy.limits_text() 生成而不是写死在这里 —— 它和真正执行校验的
    代码读同一份配置。硬约束只有一个来源，prompt 里再抄一遍迟早会和代码不一致，
    而不一致的那天，agent 会理直气壮地提交一个必然被拒的提案。
    """
    return f"""# 今日决策材料

生成时间：{now_et:%Y-%m-%d %H:%M} 美东时间
信号日：**{state['signal_date']}**（今天收盘后决策，明天开盘后执行）

> 表里所有数字都是从 IBKR 复权日线算出来的事实。
> 你只能引用这里出现过的数字。表里没有的，就是你不知道的。

## 〇、硬约束（违反任何一条，整份提案作废，今天不交易）

{limits}

## 一、你的账户

{portfolio_block(state)}

## 二、当前持仓的行情

{stats_table(held_stats) if held_stats else "_（空仓，无持仓行情）_"}

## 三、市场环境（基准，不可交易）

{regime_block(bench_stats)}

## 四、你最近几天的决策

{journal_block(recent_journal())}

## 五、候选标的

_（还没有。用 fetch 命令提名代码，通过资格审查的会追加到这里。）_
"""
