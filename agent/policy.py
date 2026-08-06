"""
提案校验 —— 这个文件是整套 AI 交易里唯一真正拦得住 agent 的东西。

prompt 是建议，代码是法律。这句话不是修辞：

  你在 prompt 里写"单票不要超过 33%"，agent 大部分时候会听。
  但它有一天会算错、会被自己的推理说服、会在长上下文里忘掉这一条 ——
  而你不会在那一天正好盯着看。

所以约束必须在提案变成订单的路径上，以代码的形式存在。

失败处理只有一种：**整份作废，当天不交易**。

不做"截断到上限后继续"，因为截断出来的组合是 agent 从没考虑过的东西。
它可能本来打算 40% AAPL + 60% 现金，截断成 33% AAPL + 67% 现金 ——
仓位结构变了，而那个变化没有经过任何人的判断。
不交易 = 保持昨天的仓位，那是唯一一个我们确定有人认可过的状态。

另外注意 check_weights 被 05_daily_job.py 的 trade 阶段又调了一次。
不是冗余：那一层堵的是"agent 绕过 commit 直接写 pending.json"。
校验放在数据被使用的地方，而不只是被产生的地方。
"""

from __future__ import annotations

import logging
import math

import config
from .screen import DENY_SYMBOLS

log = logging.getLogger("agent.policy")

# 小于这个权重的仓位没有意义：下游 weights_to_orders 的 min_trade_value
# 和 band 会把它过滤掉，结果是"提案里写了但永远不会成交"，
# 于是对账天天报偏差。要么给够，要么给 0。
MIN_MEANINGFUL_WEIGHT = 0.01

REQUIRED_KEYS = ("signal_date", "target_weights", "theses")


def one_way_turnover(target: dict[str, float], current: dict[str, float]) -> float:
    """单边换手 = Σ|Δw| / 2。口径的理由见 config.AGENT_MAX_TURNOVER 的注释。"""
    syms = set(target) | set(current)
    return sum(abs(target.get(s, 0.0) - current.get(s, 0.0)) for s in syms) / 2.0


def check_weights(weights: dict[str, float]) -> list[str]:
    """
    只看权重本身的硬边界。不需要知道当前持仓，所以 trade 阶段也能复核。

    返回违规列表，空 = 通过。
    """
    bad: list[str] = []

    for sym, w in weights.items():
        if not isinstance(w, (int, float)) or not math.isfinite(float(w)):
            bad.append(f"{sym} 的权重不是有限数字：{w!r}")
            continue
        w = float(w)
        if w < 0:
            bad.append(f"{sym} 权重 {w:.2%} 是负数 —— 只做多，不做空")
        if w > config.AGENT_MAX_WEIGHT + 1e-9:
            bad.append(f"{sym} 权重 {w:.2%} 超过单标的上限 "
                       f"{config.AGENT_MAX_WEIGHT:.0%}")
        if 0 < w < MIN_MEANINGFUL_WEIGHT:
            bad.append(f"{sym} 权重 {w:.2%} 太小 —— 低于 "
                       f"{MIN_MEANINGFUL_WEIGHT:.0%} 的仓位会被下单过滤掉，"
                       f"结果是提案和实际持仓永远对不上。要么给够，要么给 0")
        if sym in DENY_SYMBOLS:
            bad.append(f"{sym} 在杠杆/反向/波动率产品黑名单里")
        if sym in config.AGENT_DENY:
            bad.append(f"{sym} 在 .env 的 AGENT_DENY 里")

    held = {s: float(w) for s, w in weights.items() if float(w) > 0}
    if len(held) > config.AGENT_MAX_POSITIONS:
        bad.append(f"持有 {len(held)} 个标的，超过上限 "
                   f"{config.AGENT_MAX_POSITIONS} 个")

    gross = sum(held.values())
    if gross > config.AGENT_MAX_GROSS + 1e-6:
        bad.append(f"总仓位 {gross:.1%} 超过上限 {config.AGENT_MAX_GROSS:.0%}"
                   f"（不允许融资）")

    return bad


def check_proposal(
    proposal: dict,
    approved: set[str],
    reduce_only: set[str],
    current_weights: dict[str, float],
    signal_date: str,
) -> list[str]:
    """
    完整校验。

    approved      今天通过资格审查的候选池（agent 可以自由买入的范围）
    reduce_only   在持但今天没通过审查的标的：只允许减仓，不允许加仓
                  （流动性掉下去的票不该再往里加钱，但也不该强制清仓，
                   那等于把择时权交给了一个阈值）
    """
    bad: list[str] = []

    missing = [k for k in REQUIRED_KEYS if k not in proposal]
    if missing:
        return [f"提案缺少必需字段：{missing}。"
                f"格式要求见 prompts/agent_trader.md。"]

    if str(proposal["signal_date"]) != signal_date:
        bad.append(f"提案写的 signal_date 是 {proposal['signal_date']}，"
                   f"但今天的信号日是 {signal_date} —— "
                   f"这多半是复用了旧提案，拒绝执行")

    raw = proposal["target_weights"]
    if not isinstance(raw, dict):
        return [f"target_weights 必须是 {{代码: 权重}} 对象，收到 {type(raw).__name__}"]

    weights = {str(k).strip().upper(): v for k, v in raw.items()}
    bad += check_weights(weights)

    theses = proposal.get("theses") or {}
    allowed = approved | reduce_only

    for sym, w in weights.items():
        w = float(w) if isinstance(w, (int, float)) and math.isfinite(float(w)) else 0.0
        if w <= 0:
            continue
        if sym not in allowed:
            bad.append(f"{sym} 不在今天的候选池里。想买它就得先用 fetch 让它过资格审查 —— "
                       f"没有数据支撑的买入不允许发生")
        elif sym in reduce_only and w > current_weights.get(sym, 0.0) + 1e-9:
            bad.append(f"{sym} 今天没通过资格审查（流动性/价格/数据），"
                       f"只能减仓到 {current_weights.get(sym, 0.0):.1%} 以下，"
                       f"不能加到 {w:.1%}")
        if not str(theses.get(sym, "")).strip():
            bad.append(f"{sym} 有仓位但没写买入理由。每一个持仓都要有理由 —— "
                       f"明天的你要靠它判断逻辑还成不成立")

    # 卖掉的东西也要交代。不强制，但记下来，journal 里能看到
    turnover = one_way_turnover(
        {k: float(v) for k, v in weights.items()
         if isinstance(v, (int, float)) and math.isfinite(float(v))},
        current_weights,
    )
    if turnover > config.AGENT_MAX_TURNOVER + 1e-6:
        bad.append(f"单边换手 {turnover:.1%} 超过上限 "
                   f"{config.AGENT_MAX_TURNOVER:.0%}。"
                   f"想大幅调整就分几天做，一天推倒重来不允许")

    return bad


# ---------------------------------------------------------------- 熔断

HALT_FILE = config.AGENT_DIR / "HALT"


def halted() -> str | None:
    """
    人工急停。`echo 停一下 > data/agent/HALT` 就能让整条 AI 链路停下来。

    要有一个不需要改代码、不需要动计划任务、半夜也能按的开关。
    删掉文件即恢复。
    """
    if not HALT_FILE.exists():
        return None
    reason = HALT_FILE.read_text(encoding="utf-8", errors="replace").strip()
    return reason or "（HALT 文件存在，但没写原因）"


def drawdown_breached(equity: "list[float] | None") -> tuple[float | None, bool]:
    """
    净值回撤熔断。返回 (当前回撤, 是否触发)。

    没有回测的策略拿不出"历史最大回撤是多少"这种话，所以唯一能做的风控
    就是规定一个绝对的止损线：亏到这个程度就停下来，人工看一眼再决定。
    """
    if not equity:
        return None, False
    peak = max(equity)
    if peak <= 0:
        return None, False
    dd = equity[-1] / peak - 1.0
    return dd, dd < -abs(config.AGENT_MAX_DRAWDOWN)


def limits_text() -> str:
    """给 agent 看的约束清单。和上面的检查读同一份配置，不会说一套做一套。"""
    return "\n".join([
        f"- 单标的权重上限：**{config.AGENT_MAX_WEIGHT:.0%}**",
        f"- 最多同时持有：**{config.AGENT_MAX_POSITIONS} 个**标的",
        f"- 总仓位上限：**{config.AGENT_MAX_GROSS:.0%}**（不允许融资，剩下的是现金）",
        f"- 单边换手上限：**{config.AGENT_MAX_TURNOVER:.0%}/天**"
        f"（Σ|权重变化|÷2，空仓建满整个组合刚好是 50%）",
        f"- 单个仓位不得低于 **{MIN_MEANINGFUL_WEIGHT:.0%}**（要么给够，要么给 0）",
        "- **只做多，不做空**。权重必须 ≥ 0",
        "- 只能买今天通过资格审查的候选池里的标的",
        "- 每个有仓位的标的都必须写买入理由",
    ])
