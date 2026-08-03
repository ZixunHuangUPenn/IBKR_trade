"""
第 5 课：把手动跑的脚本变成无人值守的日常作业。

    python scripts/05_daily_job.py --stage signal --strategy 横截面动量
    python scripts/05_daily_job.py --stage trade            # 默认预演
    python scripts/05_daily_job.py --stage trade --execute  # 真下

为什么拆成两段，而不是像 04 那样一口气做完？

因为回测里的 lag=1 —— "t 日收盘算信号，t+1 日持有"（见 backtest/engine.py）。
04 是"算完立刻下单"，手动跑时你自己掌握时点所以无所谓；一旦挂成定时任务，
这个时序就必须钉死，否则实盘和回测的差异是往**乐观**方向偏的，最难发现。

    signal 阶段（收盘后跑）：更新数据 -> 算目标权重 -> 写 data/signals/pending.json
    trade  阶段（次日开盘后跑）：读 pending.json -> 下单 -> 对账 -> 归档

顺带一个好处：信号先落盘，你晚上可以先看一眼，第二天才真的执行。

设计上的几条硬规矩：
  - 任何一步不确定，就什么都不做。没信号 = 不交易，比按错信号交易安全得多。
  - 退出码有意义：0=正常（含"今天本来就不该做事"），1=出问题了。任务计划器看这个。
  - 全程写文件日志。出事之后能复盘，比出事当时能看见更重要。
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
from ibkr import account as acct
from ibkr.connection import IBConnection
from ibkr.execution import cancel_all, execute_plan, weights_to_orders
from ibkr.market_data import (download_universe, load_universe_prices,
                              make_contract)
from ibkr.reconcile import reconcile
from notify import notify
from scripts_util import parse_params
from strategies import REGISTRY, build

ET = ZoneInfo("America/New_York")
PENDING = config.SIGNAL_DIR / "pending.json"

log = logging.getLogger("daily_job")

# 中文 Windows 的控制台是 GBK 的，日志里一个 ✗ 就能抛 UnicodeEncodeError 把作业弄崩。
# 一个纯粹的显示问题不该有能力中断交易 —— 在 import 期就收口，连 argparse 的报错都盖住。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass


def setup_logging(stage: str) -> Path:
    """同时写控制台和文件。文件按天分，日志本身就是你的运维记录。"""
    path = config.LOGS_DIR / f"{datetime.now(ET):%Y-%m-%d}_{stage}.log"
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

    # ib_async 在 INFO 级别会把整个 Trade 对象（含全部 fills 和状态流水）打进日志。
    # 实测一次三笔的调仓产生 96 KB，其中 89 KB 是这个 —— 你要看的东西被彻底淹掉。
    # 日志是出事之后唯一的证据，可读性不是锦上添花。
    logging.getLogger("ib_async").setLevel(logging.WARNING)
    return path


# ---------------------------------------------------------------- signal 阶段

def stage_signal(args) -> int:
    now_et = datetime.now(ET)
    log.info("signal 阶段启动，美东时间 %s（周%d）%s", now_et.strftime("%Y-%m-%d %H:%M"),
             now_et.isoweekday(), "[dry-run 验证模式]" if args.dry_run else "")

    # dry-run 只是为了在搭建当天（很可能是周末）验证管道能不能跑通。
    # 它跑完整流程但不写 pending.json，所以永远不可能导致一笔交易。
    if now_et.weekday() >= 5 and not args.dry_run:
        log.info("美东是周末，美股不开市。不产生信号，正常退出。")
        return 0

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    symbols = symbols or config.DEFAULT_UNIVERSE

    # 只读连接：这一步只下数据，从连接层就断掉下单的可能
    with IBConnection(readonly=True, client_id=config.JOB_CLIENT_ID,
                      retries=config.CONNECT_RETRIES) as ib:
        data = download_universe(ib, symbols, duration=args.duration,
                                 bar_size="1 day", what_to_show="ADJUSTED_LAST")

    if len(data) < len(symbols):
        missing = sorted(set(symbols) - set(data))
        # 标的池缺一块，算出来的横截面排名就是错的。宁可今天不交易。
        notify("信号作业中止：数据不全", f"缺少 {missing}", level="error")
        return 1

    prices = load_universe_prices(symbols)
    last_bar = prices.index[-1].date()

    if last_bar != now_et.date() and not args.dry_run:
        # 工作日但没有今天的日线：要么是美股假日，要么 IBKR 的日线还没结算出来。
        # 两种情况都不该硬着头皮出信号 —— 那会拿昨天的收盘当今天用。
        log.warning("最后一根日线是 %s，不是今天（%s）。可能是美股假日，"
                    "也可能是收盘后跑得太早。不产生信号。", last_bar, now_et.date())
        notify("今日无信号", f"最后一根日线 {last_bar}，非今日。检查是否假日或作业时间过早。",
               level="warn")
        return 0

    params = parse_params(args.params)
    strat = build(args.strategy, **params)
    weights = {k: round(float(v), 6)
               for k, v in strat.latest_weights(prices).items() if abs(v) > 1e-9}

    # 策略自己声明的调仓日历。回测严格遵守它（run_backtest 的 rebalance_mask），
    # 实盘不遵守的话，跑的就不是被回测过的那个策略 ——
    # 横截面动量实测：月末调仓年换手 359%，改成日频+1%带宽是 2222%，差 6 倍多。
    # 这种偏差不会体现在收益上，只会体现在成本上，所以特别难被发现。
    mask = strat.rebalance_mask(prices.index)
    rebalance_day = True if mask is None else bool(mask.iloc[-1])

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "signal_date": str(last_bar),
        "strategy": args.strategy,
        "params": params,
        "label": strat.label,
        "universe": symbols,
        "weights": weights,
        "rebalance_day": rebalance_day,
        "ref_close": {s: round(float(prices[s].iloc[-1]), 4)
                      for s in prices.columns},
    }

    ranked = sorted(weights.items(), key=lambda x: -x[1])
    log.info("策略 %s | 信号日 %s | 调仓日=%s | 目标权重: %s", strat.label, last_bar,
             "是" if rebalance_day else "否",
             {k: f"{v:.2%}" for k, v in ranked} or "（空仓）")

    if args.dry_run:
        log.info("dry-run：管道跑通了，但没有写 %s，因此不会产生任何交易。", PENDING.name)
        return 0

    PENDING.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("信号已写入 %s", PENDING)
    notify(f"今日信号已生成 [{strat.label}]",
           f"信号日 {last_bar}\n" + "\n".join(f"{k} {v:.2%}" for k, v in ranked))
    return 0


# ----------------------------------------------------------------- trade 阶段

def _market_closed_today(ib, symbol: str = "SPY") -> bool | None:
    """
    今天美股开不开市？问 IBKR 要交易时段，别自己维护假日表 —— 那张表迟早会过期。

    liquidHours 形如 "20260803:0930-20260803:1600;20260804:CLOSED"。
    返回 True=休市 / False=开市 / None=判断不了。

    判断不了时返回 None 而不是 True：让作业继续跑。
    "静悄悄地什么都没做"比"下了单但没成交"更难发现 ——
    后者对账会报警，前者只有你自己某天想起来去翻日志才会发现。
    """
    try:
        details = ib.reqContractDetails(make_contract(symbol))
        if not details:
            return None
        hours = details[0].liquidHours or details[0].tradingHours or ""
        today = datetime.now(ET).strftime("%Y%m%d")
        for seg in hours.split(";"):
            if seg.startswith(today + ":"):
                return "CLOSED" in seg.upper()
        return None
    except Exception as e:  # noqa: BLE001
        log.warning("取不到交易时段（%s），跳过开市检查", e)
        return None


def _live_prices(ib, symbols: list[str], fallback: dict[str, float]) -> dict[str, float]:
    """取当前价；拿不到就退回参考收盘价，但要吭声。"""
    contracts = [make_contract(s) for s in symbols]
    ib.qualifyContracts(*contracts)
    out = {}
    for t in ib.reqTickers(*contracts):
        px, sym = t.marketPrice(), t.contract.symbol
        if px and px == px and px > 0:
            out[sym] = px
        else:
            out[sym] = fallback[sym]
            log.warning("%s 取不到实时价，退回参考收盘价 %.2f（跳空大的日子会算错数量）",
                        sym, fallback[sym])
    return out


def _archive(sig: dict, signal_date, **extra) -> None:
    """
    消费信号：写归档 + 清空 pending。

    "不交易"也必须消费。否则明天 pending 里还躺着今天的旧信号，
    而新的信号又会把它覆盖 —— 两种情况下 pending 的语义都不再是
    "待执行的最新信号"，后面所有的年龄检查就都失去意义了。
    """
    archive = config.SIGNAL_DIR / f"{signal_date}_{sig['strategy']}.json"
    sig["consumed_at"] = datetime.now(timezone.utc).isoformat()
    sig.update(extra)
    archive.write_text(json.dumps(sig, indent=2, ensure_ascii=False, default=str),
                       encoding="utf-8")
    PENDING.unlink()
    log.info("信号已归档到 %s，pending 已清空。", archive.name)


def stage_trade(args) -> int:
    now_et = datetime.now(ET)
    log.info("trade 阶段启动，美东时间 %s", now_et.strftime("%Y-%m-%d %H:%M"))

    # 周末也会到点触发。周五的信号这时还躺在 pending 里，不拦住的话会往关着的
    # 市场发单，而且信号被消费归档 —— 周一反而不交易了。
    if now_et.weekday() >= 5:
        log.info("美东是周末，美股不开市。保留待执行信号，正常退出。")
        return 0

    if not PENDING.exists():
        log.info("没有待执行信号（%s 不存在）。今天不该交易，正常退出。", PENDING.name)
        return 0

    sig = json.loads(PENDING.read_text(encoding="utf-8"))
    signal_date = datetime.fromisoformat(sig["signal_date"]).date()
    age = (now_et.date() - signal_date).days

    if age > config.MAX_SIGNAL_AGE_DAYS:
        # 信号作业已经挂了好几天。按陈旧信号下单比不下单危险得多。
        msg = (f"信号日 {signal_date} 距今 {age} 天，超过上限 "
               f"{config.MAX_SIGNAL_AGE_DAYS} 天。拒绝执行。"
               f"\n先查 signal 阶段为什么没跑。")
        log.error(msg)
        notify("拒绝交易：信号过期", msg, level="error")
        return 1
    if age <= 0:
        # 同一天里 signal 和 trade 都跑了 = lag=0，等于把回测里的时序破坏掉了
        msg = (f"信号日 {signal_date} 就是今天。signal 和 trade 不该在同一个交易日执行"
               f"（回测约定 lag=1）。拒绝执行。")
        log.error(msg)
        notify("拒绝交易：时序不对", msg, level="error")
        return 1

    target = sig["weights"]
    log.info("待执行信号：%s | 信号日 %s（%d 天前）| 目标 %s",
             sig["label"], signal_date, age,
             {k: f"{v:.2%}" for k, v in target.items()} or "（空仓）")

    # 遵守策略自己的调仓日历。不遵守的话，实盘跑的就不是被回测过的那个策略：
    # 横截面动量按月末调仓年换手 359%，按日频+1%带宽是 2222%。
    # 收益看不出差别，成本差 6 倍 —— 这种偏差最难被发现。
    if not sig.get("rebalance_day", True) and not args.ignore_calendar:
        log.info("策略 %s 的调仓日历说信号日 %s 不是调仓日，今天不交易。"
                 "（要覆盖它加 --ignore-calendar，但先想清楚回测还算不算数）",
                 sig["label"], signal_date)
        if args.execute:
            _archive(sig, signal_date, skipped="非调仓日")
        return 0

    if args.execute and config.is_live_port() and not config.ALLOW_LIVE:
        log.error("拒绝：--execute + 实盘端口，但 IB_ALLOW_LIVE=false。")
        return 1

    readonly = config.READONLY and not args.execute
    with IBConnection(readonly=readonly, client_id=config.JOB_CLIENT_ID,
                      retries=config.CONNECT_RETRIES) as ib:
        if _market_closed_today(ib):
            # 美股假日。信号不消费，留到下一个交易日执行（年龄检查会兜住太久的情况）。
            log.info("今天美股休市。保留待执行信号，正常退出。")
            return 0

        nav = acct.account_summary(ib)["NetLiquidation"]
        pos_df = acct.positions_df(ib)
        current = dict(zip(pos_df["symbol"], pos_df["position"])) if not pos_df.empty else {}
        log.info("账户净值 $%s | 当前持仓 %s", f"{nav:,.2f}", current or "（空仓）")

        # 开盘前先清掉隔夜残留的挂单，否则它们会和今天的新单叠加成超额仓位
        stale = acct.open_orders_df(ib)
        if not stale.empty:
            log.warning("发现 %d 笔遗留未成交订单：\n%s", len(stale), stale.to_string(index=False))
            if args.execute:
                cancel_all(ib)
                ib.sleep(3)

        need = sorted(set(target) | set(current))
        fallback = {s: float(v) for s, v in sig["ref_close"].items()}
        missing = [s for s in need if s not in fallback]
        if missing:
            log.warning("这些持仓不在信号的标的池里，无法定价，将被忽略：%s", missing)
            need = [s for s in need if s in fallback]

        px = _live_prices(ib, need, fallback)

        plans = weights_to_orders(
            target_weights=target, current_positions=current, prices=px, nav=nav,
            min_trade_value=args.min_trade, drift_threshold=args.band,
        )

        result = execute_plan(ib, plans, nav=nav, dry_run=not args.execute,
                              order_type=args.order_type)

        if not args.execute:
            log.info("这是预演，没有真的下单。加 --execute 才会执行。")
            return 0

        # 等订单落定再对账。等太短会把"还在路上"误判成"没成交"。
        ib.sleep(args.settle_seconds)

        # 容忍度必须盖住"我们故意不交易"的那部分，否则天天误报：
        #   band       —— 偏离小于它就不动手
        #   min_trade  —— 金额太小不值得交易（小账户尤其常见）
        #   整股取整   —— weights_to_orders 向下取整，每个标的最多残留 1 股
        tol = max(config.RECONCILE_TOLERANCE, args.band,
                  args.min_trade / nav if nav > 0 else 0.0)
        report = reconcile(ib, target, prices=px, tolerance=tol)
        log.info("对账结果：\n%s", report.text())
        acct.save_snapshot(ib)

    _archive(sig, signal_date,
             orders=result.to_dict("records") if not result.empty else [],
             reconcile_ok=report.ok, reconcile_problems=report.problems)

    n = len(result) if not result.empty else 0
    if report.ok:
        notify(f"调仓完成：{n} 笔", report.text())
    else:
        notify(f"调仓完成但对账不通过：{n} 笔", report.text(), level="error")
        return 1
    return 0


# ------------------------------------------------------------------------ 入口

def main() -> int:
    ap = argparse.ArgumentParser(description="无人值守日常作业")
    ap.add_argument("--stage", required=True, choices=["signal", "trade"])
    # signal 阶段
    ap.add_argument("--strategy", help=f"signal 阶段必填。可选: {list(REGISTRY)}")
    ap.add_argument("--params", nargs="*", default=[])
    ap.add_argument("--symbols", default="", help="留空 = config.DEFAULT_UNIVERSE")
    ap.add_argument("--duration", default="5 Y")
    ap.add_argument("--dry-run", action="store_true",
                    help="signal 阶段：跑完整流程但不写信号文件，用于搭建当天验证管道")
    # trade 阶段
    ap.add_argument("--execute", action="store_true", help="真正发送订单")
    ap.add_argument("--order-type", default="LMT", choices=["LMT", "MKT"])
    ap.add_argument("--band", type=float, default=0.01)
    ap.add_argument("--min-trade", type=float, default=200.0)
    ap.add_argument("--settle-seconds", type=float, default=10.0,
                    help="下单后等多久再对账")
    ap.add_argument("--ignore-calendar", action="store_true",
                    help="无视策略声明的调仓日历，每天都调。会让实盘换手率显著偏离回测")
    args = ap.parse_args()

    if args.stage == "signal" and not args.strategy:
        ap.error("--stage signal 需要 --strategy")

    logfile = setup_logging(args.stage)
    log.info("=" * 60)
    log.info("环境: %s:%s (%s) | 日志: %s", config.HOST, config.PORT,
             "实盘" if config.is_live_port() else "Paper", logfile)

    try:
        rc = stage_signal(args) if args.stage == "signal" else stage_trade(args)
    except Exception as e:  # noqa: BLE001 - 作业的最外层，任何异常都必须变成告警
        log.exception("作业异常终止")
        notify(f"{args.stage} 作业异常终止", f"{type(e).__name__}: {e}", level="error")
        return 1

    log.info("作业结束，退出码 %d", rc)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
