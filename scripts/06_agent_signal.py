"""
第 6 课：让 agent 每天自己选股 —— 同时不把安全网拆掉。

    python scripts/06_agent_signal.py --stage prepare            # ① 备料（Python 跑）
    python scripts/06_agent_signal.py --stage fetch --symbols AAPL,MSFT,NVDA   # ② agent 调
    python scripts/06_agent_signal.py --stage commit             # ③ 校验并落信号（Python 跑）

三个阶段，中间夹着一次 claude -p。完整编排见 scripts/run_agent.ps1。

    prepare  账户状态 + 持仓行情 + 市场环境 + 最近几天的决策 -> data/agent/context.md
      ↓
    claude   读 context.md，用 fetch 提名候选并拿数据，写 data/agent/proposal.json
      ↓
    commit   拿 agent/policy.py 逐条校验提案，通过才写 data/signals/pending.json
      ↓
    05_daily_job.py --stage trade    次日开盘后执行（这一步完全没改）

整套设计只围绕一句话：**agent 只能写提案，写不了订单。**

它没有 IBKR 连接，没有下单能力，唯一的产出是一个 JSON 文件。那个文件要变成
订单，必须先过 policy 的硬约束，再过 05 的熔断、开市检查、残单清理、对账。
agent 那一环坏掉（幻觉、算错、被自己说服）的最坏结果是"今天不交易"。

和 05 的 signal 阶段一样遵守 lag=1：今天收盘后决策，明天开盘后执行。
理由见 05_daily_job.py 开头 —— 不遵守的话，实盘和回测的偏差往乐观方向走。

退出码：0=继续  10=今天不该做事（正常跳过）  1=出问题了
"""

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
from agent import datapack, policy, screen
from ibkr import account as acct
from ibkr.connection import IBConnection
from ibkr.market_data import download_bars, load_bars
from notify import notify

ET = ZoneInfo("America/New_York")

PENDING = config.SIGNAL_DIR / "pending.json"
CONTEXT = config.AGENT_DIR / "context.md"
STATE = config.AGENT_DIR / "state.json"
CANDIDATES = config.AGENT_DIR / "candidates.json"
PROPOSAL = config.AGENT_DIR / "proposal.json"

SKIP = 10   # "今天本来就不该做事"，和"出错了"必须能分开

log = logging.getLogger("agent_job")

# 中文 Windows 控制台是 GBK 的，日志里一个特殊字符就能抛 UnicodeEncodeError
# 把作业弄崩。纯显示问题不该有能力中断交易 —— 在 import 期就收口。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass


def setup_logging(stage: str) -> Path:
    path = config.LOGS_DIR / f"{datetime.now(ET):%Y-%m-%d}_agent_{stage}.log"
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()

    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)

    logging.getLogger("ib_async").setLevel(logging.WARNING)
    return path


def _load(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        log.error("%s 不是合法 JSON：%s", path.name, e)
        return None


def _dump(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False, default=str),
                    encoding="utf-8")


# ---------------------------------------------------------------- prepare

def stage_prepare(args) -> int:
    now_et = datetime.now(ET)
    log.info("prepare 阶段启动，美东时间 %s %s", now_et.strftime("%Y-%m-%d %H:%M"),
             "[dry-run 验证模式]" if args.dry_run else "")

    if now_et.weekday() >= 5 and not args.dry_run:
        log.info("美东是周末，美股不开市。今天不决策。")
        return SKIP

    reason = policy.halted()
    if reason:
        msg = f"data/agent/HALT 存在，AI 链路已人工停用：{reason}\n删掉该文件即恢复。"
        log.warning(msg)
        notify("AI 交易已人工停用", msg, level="warn")
        return SKIP

    # 回撤熔断要在连 IBKR 之前查 —— 用的是历史快照，不需要连接，
    # 而且熔断时连都不用连，少一次出错的机会。
    equity = acct.load_equity_curve()
    dd, breached = policy.drawdown_breached(
        equity["net_liquidation"].dropna().tolist() if not equity.empty else None)
    if breached and not args.dry_run:
        msg = (f"净值距历史高点 {dd:.1%}，已跌破熔断线 "
               f"-{config.AGENT_MAX_DRAWDOWN:.0%}。AI 决策已停止。\n"
               f"这不是让你调大阈值的信号。先人工看一遍最近的 journal，"
               f"想清楚是策略失效还是正常波动，再决定要不要继续。\n"
               f"确认继续：删掉 data/agent/HALT（如果有）并调整 AGENT_MAX_DRAWDOWN。")
        log.error(msg)
        notify("AI 交易熔断：回撤超限", msg, level="error")
        policy.HALT_FILE.write_text(
            f"净值回撤 {dd:.1%} 触发熔断（{now_et:%Y-%m-%d}）。人工确认后删除本文件。",
            encoding="utf-8")
        return SKIP

    with IBConnection(readonly=True, client_id=config.JOB_CLIENT_ID,
                      retries=config.CONNECT_RETRIES) as ib:
        summary = acct.account_summary(ib)
        nav = summary["NetLiquidation"]
        pos_df = acct.positions_df(ib)
        held = sorted(pos_df["symbol"].tolist()) if not pos_df.empty else []
        log.info("账户净值 $%s | 持仓 %s", f"{nav:,.2f}", held or "（空仓）")

        # 基准和持仓的日线必须刷新。
        #
        # 持仓这一条特别关键：agent 明天可能要清掉某个标的，而 05 的 trade 阶段
        # 是拿 pending.json 里的 ref_close 定价的 —— 没有价格的标的会被它
        # "静默忽略"（见 stage_trade 里的 missing 分支）。那意味着你以为下了清仓单，
        # 实际上那个仓位一直躺在账户里，而日志只有一行 warning。
        need_bars = sorted(set(datapack.BENCHMARKS) | set(held))
        log.info("刷新日线：%s", need_bars)
        fresh = {}
        for sym in need_bars:
            try:
                fresh[sym] = download_bars(ib, sym, duration="3 Y", bar_size="1 day",
                                           what_to_show="ADJUSTED_LAST")
            except Exception as e:  # noqa: BLE001
                log.error("%s 日线刷新失败：%s", sym, e)

        # 在持但今天审不过的标的：允许继续拿着、允许减，但不允许加仓。
        # 直接强制清仓是不对的 —— 那等于把择时权交给一个流动性阈值。
        # 把刚下好的 bars 传进去，别让它再下一遍（IBKR 限速额度很紧）。
        reduce_only, held_ok = [], {}
        for sym in held:
            ok, why, bars = screen.screen_symbol(ib, sym, bars=fresh.get(sym))
            (log.info if ok else log.warning)("持仓审查 %s", why)
            if ok:
                held_ok[sym] = bars
            else:
                reduce_only.append(sym)

    try:
        spy = load_bars("SPY")["close"].astype(float)
    except FileNotFoundError:
        log.error("SPY 没有缓存，市场环境和相对强弱都算不出来。")
        spy = None

    # 日线新鲜度检查。要求分两种情况，混成一条会在其中一种下判错：
    #
    #   收盘（16:00 ET）之后跑 —— 正常作业时点。必须有**今天**的日线，
    #       没有就说明是美股假日，或者跑得太早日线还没结算。
    #       这时硬出信号 = 拿昨天的收盘当今天用。
    #
    #   收盘之前跑 —— 手动补跑、或者作业拖过了午夜。这时最新的日线**本来就
    #       只能是上一个交易日**，要求"必须是今天"是错的：那一场还没开盘。
    #       而"用上一个交易日的收盘决策、下一个开盘执行"恰恰就是 lag=1 本身。
    #
    # 第一版把两种情况写成了一条，结果凌晨补跑永远被拒，而拒绝理由
    # （"可能是假日"）看起来完全合理 —— 这种错最难发现。
    if spy is not None and not args.dry_run:
        last_bar = spy.index[-1].date()
        after_close = now_et.hour >= 16
        stale_days = (now_et.date() - last_bar).days

        if after_close and last_bar != now_et.date():
            log.warning("SPY 最后一根日线是 %s，不是今天（%s）。已过收盘时点却没有"
                        "今天的日线：可能是美股假日，也可能跑得太早。今天不决策。",
                        last_bar, now_et.date())
            notify("AI 今日不决策", f"SPY 最后一根日线 {last_bar}，非今日。"
                                    f"检查是否假日或作业时间过早。", level="warn")
            return SKIP

        if stale_days > config.MAX_SIGNAL_AGE_DAYS:
            # 数据本身就旧了。周末+假日最多 3 天，超过说明数据管道断了好几天，
            # 按这种数据做决策比不做危险得多。
            log.error("SPY 最后一根日线 %s 距今 %d 天，超过上限 %d 天。"
                      "数据管道可能已经断了，今天不决策。",
                      last_bar, stale_days, config.MAX_SIGNAL_AGE_DAYS)
            notify("AI 今日不决策：数据过旧",
                   f"SPY 最后一根日线 {last_bar}，距今 {stale_days} 天。", level="error")
            return SKIP

        if not after_close:
            log.info("当前是美东 %s（收盘前），最新日线只可能是上一个交易日。"
                     "以 %s 的收盘作为信号日，下一个开盘执行 —— 这就是 lag=1。",
                     now_et.strftime("%H:%M"), last_bar)

    signal_date = str(spy.index[-1].date()) if spy is not None else str(now_et.date())

    # ------- 组合状态。权重一律以 NAV 为分母（positions_df 的 weight 是以持仓
    # 市值为分母的，两者在有现金时差别很大，混用会让换手率算错）。
    positions, current_weights, ref_close = [], {}, {}
    for r in pos_df.to_dict("records") if not pos_df.empty else []:
        sym = r["symbol"]
        price = r.get("market_price")
        if price is None or price != price or price <= 0:
            # portfolio() 拿不到价时会是 NaN，退回缓存收盘价，但要吭声
            try:
                price = float(load_bars(sym)["close"].iloc[-1])
                log.warning("%s 取不到实时价，退回缓存收盘价 %.2f", sym, price)
            except Exception:  # noqa: BLE001
                log.error("%s 既无实时价也无缓存价，无法计入组合状态", sym)
                continue
        mv = r["position"] * price
        positions.append({**r, "market_price": price, "market_value": mv,
                          "weight": mv / nav if nav else 0.0})
        current_weights[sym] = mv / nav if nav else 0.0
        ref_close[sym] = round(float(price), 4)

    gross = sum(current_weights.values())
    state = {
        "signal_date": signal_date,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "nav": nav,
        "cash": summary.get("TotalCashValue", nav * (1 - gross)),
        "gross": gross,
        "drawdown": dd,
        "positions": positions,
        "current_weights": {k: round(v, 6) for k, v in current_weights.items()},
        "reduce_only": reduce_only,
        "ref_close": ref_close,
    }

    held_stats = [datapack.compute_stats(s, b, spy) for s, b in held_ok.items()]
    for sym in reduce_only:                       # 审不过的也要给数据，否则没法决定减多少
        try:
            held_stats.append(datapack.compute_stats(sym, load_bars(sym), spy))
        except Exception:  # noqa: BLE001
            log.warning("%s 无缓存日线，本次不提供它的行情", sym)

    bench_stats = []
    for sym in datapack.BENCHMARKS:
        try:
            bench_stats.append(datapack.compute_stats(sym, load_bars(sym), spy))
        except Exception as e:  # noqa: BLE001
            log.warning("基准 %s 算不出来：%s", sym, e)

    # 上一轮的候选和提案必须清掉。留着的话 commit 可能吃到昨天的提案 ——
    # 那是"按陈旧信号交易"的另一种形式，而且比信号过期更隐蔽，
    # 因为文件里的 signal_date 看起来完全正常。
    # 通过审查的持仓要直接进候选池，否则 agent 连"继续持有 SPY"都会被判违规
    # （check_proposal 要求有仓位的标的必须在候选池或 reduce_only 里）。
    #
    # 但它们不占 agent 的 fetch 额度 —— 那个额度是给"新想法"的，
    # 让持仓吃掉它，等于持仓越多能看的新票越少，方向正好反了。
    _dump(STATE, state)
    _dump(CANDIDATES, {
        "signal_date": signal_date,
        "approved": {s: {"close": round(float(b["close"].iloc[-1]), 4),
                         "last_bar": str(b.index[-1].date()), "source": "持仓"}
                     for s, b in held_ok.items()},
        "attempted": sorted(set(held_ok) | set(reduce_only)),   # 去重用
        "agent_fetched": [],                                    # 额度只算这个
    })
    if PROPOSAL.exists():
        PROPOSAL.unlink()

    CONTEXT.write_text(
        datapack.build_context(state, bench_stats, held_stats, now_et,
                               policy.limits_text()),
        encoding="utf-8")

    log.info("备料完成 -> %s（信号日 %s，持仓 %d 个，只减仓 %s）",
             CONTEXT, signal_date, len(positions), reduce_only or "无")
    return 0


# ---------------------------------------------------------------- fetch

FETCH_SECTION = "## 五、候选标的"


def stage_fetch(args) -> int:
    """agent 调用的唯一一个能碰 IBKR 的入口，而且是只读的。"""
    state = _load(STATE)
    if not state:
        print("错误：还没有 state.json。prepare 阶段没跑或跑失败了，"
              "今天不该有决策。")
        return 1

    cand = _load(CANDIDATES) or {"signal_date": state["signal_date"], "approved": {},
                                 "attempted": [], "agent_fetched": []}
    cand.setdefault("agent_fetched", [])

    symbols = [s.strip().upper() for s in args.symbols.replace(" ", ",").split(",")
               if s.strip()]
    if not symbols:
        print("错误：--symbols 是空的。")
        return 1

    already = set(cand["attempted"])
    new = [s for s in symbols if s not in already]
    dup = [s for s in symbols if s in already]
    if dup:
        print(f"以下代码今天已经查过，跳过：{dup}"
              f"（持仓在 prepare 阶段就审过了，不用再查）")

    # 限速与成本的双重保护。IBKR 历史数据 10 分钟约 60 次，超了是**静默拒绝**
    # （错误 162），你会以为数据下好了，其实缺了一块。见 ibkr/market_data.py。
    # 额度只算 agent 主动提名的，持仓不占 —— 理由见 prepare 里的注释。
    room = config.AGENT_MAX_CANDIDATES - len(cand["agent_fetched"])
    if room <= 0:
        print(f"今天的候选额度已用完（上限 {config.AGENT_MAX_CANDIDATES} 个）。"
              f"用已有的数据做决策。")
        return 0
    if len(new) > room:
        print(f"额度只剩 {room} 个，本次只查前 {room} 个：{new[:room]}")
        new = new[:room]

    if not new:
        print("没有新代码需要查。")
        return 0

    with IBConnection(readonly=True, client_id=config.JOB_CLIENT_ID,
                      retries=config.CONNECT_RETRIES) as ib:
        passed, notes = screen.screen_many(ib, new)

    try:
        spy = load_bars("SPY")["close"].astype(float)
    except FileNotFoundError:
        spy = None

    rows = [datapack.compute_stats(s, b, spy) for s, b in passed.items()]
    for r in rows:
        cand["approved"][r["symbol"]] = {"close": r["close"], "last_bar": r["last_bar"],
                                         "source": "候选"}
    cand["attempted"] = sorted(already | set(new))
    cand["agent_fetched"] = sorted(set(cand["agent_fetched"]) | set(new))
    _dump(CANDIDATES, cand)

    used = len(cand["agent_fetched"])

    # 追加到 context.md，让 agent 之后重读时能一次看全，不用翻自己的历史输出
    table = datapack.stats_table(rows)
    with CONTEXT.open("a", encoding="utf-8") as f:
        f.write(f"\n### 候选批次（累计已查 {used} 个）\n\n")
        f.write("\n".join(notes) + "\n\n" + table)

    print("\n".join(notes))
    print()
    print(table)
    print(f"已用额度 {used}/{config.AGENT_MAX_CANDIDATES}，"
          f"当前候选池（含持仓）：{sorted(cand['approved'])}")
    return 0


# ---------------------------------------------------------------- commit

def stage_commit(args) -> int:
    state, cand, proposal = _load(STATE), _load(CANDIDATES), _load(PROPOSAL)

    if not state:
        log.error("没有 state.json，prepare 没跑成功。不写信号。")
        return 1
    if proposal is None:
        msg = ("agent 没有产出 data/agent/proposal.json。可能是它跑挂了、超时了，"
               "或者判断今天不该动 —— 但它应该显式写出来。\n"
               "不写信号，明天维持现有仓位。")
        log.error(msg)
        notify("AI 未产出提案", msg, level="error")
        return 1

    signal_date = state["signal_date"]
    approved = set((cand or {}).get("approved", {}))
    reduce_only = set(state.get("reduce_only", []))
    current = {k: float(v) for k, v in state.get("current_weights", {}).items()}

    violations = policy.check_proposal(proposal, approved, reduce_only,
                                       current, signal_date)
    if violations:
        detail = "\n".join(f"  {i}. {v}" for i, v in enumerate(violations, 1))
        msg = (f"agent 的提案违反了 {len(violations)} 条硬约束，整份作废，"
               f"今天不交易（保持现有仓位）：\n{detail}")
        log.error(msg)
        notify("AI 提案被拒绝", msg, level="error")
        # 被拒的提案也进 journal。下次 prepare 会把它喂回给 agent，
        # 让它看见自己昨天错在哪 —— 否则同一个错误它会一直犯。
        datapack.append_journal({
            "signal_date": signal_date, "status": "rejected",
            "violations": violations,
            "weights": proposal.get("target_weights", {}),
            "theses": proposal.get("theses", {}),
        })
        return 1

    weights = {str(k).upper(): round(float(v), 6)
               for k, v in proposal["target_weights"].items() if float(v) > 1e-9}

    # ref_close 必须盖住"目标"和"现有持仓"的并集。
    # 漏掉现有持仓的价格，05 的 trade 阶段会把那个标的静默剔除 ——
    # 清仓单根本不会发出去，而你只会在日志里看到一行 warning。
    ref_close = dict(state.get("ref_close", {}))
    for sym in weights:
        if sym not in ref_close:
            try:
                ref_close[sym] = round(float(load_bars(sym)["close"].iloc[-1]), 4)
            except Exception as e:  # noqa: BLE001
                msg = f"{sym} 取不到参考价（{e}）—— 没有价格就没法下单。整份作废。"
                log.error(msg)
                notify("AI 提案被拒绝：缺少参考价", msg, level="error")
                return 1

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "signal_date": signal_date,
        "strategy": config.AGENT_STRATEGY_NAME,
        "source": "agent",                 # trade 阶段据此再校验一次权重边界
        "params": {},
        "label": config.AGENT_STRATEGY_NAME,
        "universe": sorted(set(ref_close)),
        "weights": weights,
        "rebalance_day": True,             # agent 每天都可以调，上限由换手率约束
        "ref_close": ref_close,
        "theses": proposal.get("theses", {}),
        "risk_notes": proposal.get("risk_notes", ""),
        "turnover": round(policy.one_way_turnover(weights, current), 4),
    }

    ranked = sorted(weights.items(), key=lambda x: -x[1])
    log.info("提案通过校验 | 信号日 %s | 单边换手 %.1f%% | 目标：%s",
             signal_date, payload["turnover"] * 100,
             {k: f"{v:.1%}" for k, v in ranked} or "（空仓）")
    for sym, w in ranked:
        log.info("  %s %.1f%% —— %s", sym, w * 100,
                 proposal.get("theses", {}).get(sym, ""))

    if args.dry_run:
        log.info("dry-run：校验通过，但没有写 %s，因此不会产生任何交易。", PENDING.name)
        return 0

    _dump(PENDING, payload)
    datapack.append_journal({
        "signal_date": signal_date, "status": "committed",
        "weights": weights, "theses": proposal.get("theses", {}),
        "risk_notes": proposal.get("risk_notes", ""),
        "turnover": payload["turnover"],
    })

    # 推送发摘要，不发全文。
    #
    # 第一版把五个持仓的完整 thesis 拼进去，约 4KB，Bark 那侧的 nginx
    # 直接回 413 —— 最该收到的那条通知反而没送到。
    # 推送的作用是"让你知道发生了什么、要不要去看"；完整理由在日志和
    # data/agent/journal.jsonl 里，那才是复盘该去的地方。
    lines = []
    for sym, w in ranked:
        delta = w - current.get(sym, 0.0)
        lines.append(f"{sym} {w:.0%} ({delta:+.0%})")
    for sym in sorted(set(current) - set(weights)):
        lines.append(f"{sym} 清仓 (-{current[sym]:.0%})")

    body = (f"信号日 {signal_date}｜换手 {payload['turnover']:.1%}｜"
            f"仓位 {sum(weights.values()):.0%}\n"
            + ("　".join(lines) if lines else "全部清仓")
            + "\n\n明早开盘后执行。理由见 journal.jsonl")
    notify(f"AI 今日信号：{len(weights)} 个持仓", body)
    log.info("信号已写入 %s，明天开盘后由 05_daily_job.py --stage trade 执行。", PENDING)
    return 0


# ------------------------------------------------------------------------ 入口

def main() -> int:
    ap = argparse.ArgumentParser(description="AI 自主选股作业")
    ap.add_argument("--stage", required=True, choices=["prepare", "fetch", "commit"])
    ap.add_argument("--symbols", default="", help="fetch 阶段：逗号分隔的代码")
    ap.add_argument("--dry-run", action="store_true",
                    help="prepare：无视周末/日线日期检查；commit：校验但不写信号")
    args = ap.parse_args()

    if args.stage == "fetch" and not args.symbols:
        ap.error("--stage fetch 需要 --symbols")

    # fetch 是 agent 在调，它读的是 stdout，不需要作业日志的仪式感
    if args.stage != "fetch":
        logfile = setup_logging(args.stage)
        log.info("=" * 60)
        log.info("环境: %s:%s (%s) | 日志: %s", config.HOST, config.PORT,
                 "实盘" if config.is_live_port() else "Paper", logfile)
    else:
        logging.basicConfig(level=logging.WARNING,
                            format="%(levelname)s %(name)s: %(message)s")

    try:
        rc = {"prepare": stage_prepare, "fetch": stage_fetch,
              "commit": stage_commit}[args.stage](args)
    except Exception as e:  # noqa: BLE001 - 作业最外层，任何异常都必须变成告警
        log.exception("作业异常终止")
        notify(f"AI {args.stage} 阶段异常终止", f"{type(e).__name__}: {e}", level="error")
        return 1

    if args.stage != "fetch":
        log.info("阶段结束，退出码 %d", rc)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
