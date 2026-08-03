"""
历史行情下载 + 本地缓存。

三件事新手必须知道：

1) 限速是硬约束
   IBKR 对历史数据请求限速：10 分钟内不超过 ~60 次，同一合约 15 秒内
   不要重复请求。踩线的后果是被静默拒绝（错误 162），不是报错崩溃 ——
   所以你会以为"数据下好了"，其实缺了一大块。这里强制 sleep。

2) 复权很重要
   whatToShow='TRADES' 拿到的是未复权价，分红除权日会有个假跌幅。
   拿它回测长期策略，收益会被系统性低估。
   'ADJUSTED_LAST' 是复权价 —— 但 IBKR 只在 endDateTime 为空（即"到现在"）
   时支持它，所以复权模式下不能分段拉取。这是个真实的取舍。

3) 数据要落地
   每次回测都重新连 IBKR 拉数据，既慢又浪费限速额度，而且没法复现。
   下一次、下下次跑的是不是同一批数据？落到 parquet 就确定了。
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta

import pandas as pd
from ib_async import IB, Stock, Contract, util

import config

log = logging.getLogger("ibkr.market_data")

PACING_SLEEP = 2.0  # 秒。别调小，被限速比慢几秒难受得多。

# 每个 bar size 单次请求的安全上限（IBKR 的限制表，取保守值）
_MAX_CHUNK = {
    "1 min": "2 D", "2 mins": "2 D", "3 mins": "1 W", "5 mins": "1 W",
    "15 mins": "2 W", "30 mins": "1 M",
    "1 hour": "1 M", "2 hours": "1 M", "4 hours": "1 M", "8 hours": "1 M",
    "1 day": "1 Y", "1 week": "1 Y", "1 month": "1 Y",
}

_DUR_DAYS = {"S": 1 / 86400, "D": 1, "W": 7, "M": 30, "Y": 365}


def _duration_days(dur: str) -> float:
    m = re.fullmatch(r"(\d+)\s*([SDWMY])", dur.strip().upper())
    if not m:
        raise ValueError(f"看不懂的 duration: {dur!r}（应形如 '2 Y' / '6 M' / '30 D'）")
    return int(m.group(1)) * _DUR_DAYS[m.group(2)]


def make_contract(symbol: str, sec_type: str = "STK", exchange: str = "SMART",
                  currency: str = "USD", primary: str | None = None) -> Contract:
    if sec_type == "STK":
        c = Stock(symbol, exchange, currency)
        if primary:
            c.primaryExchange = primary
        return c
    return Contract(symbol=symbol, secType=sec_type, exchange=exchange, currency=currency)


def _bars_to_df(bars) -> pd.DataFrame:
    if not bars:
        return pd.DataFrame()
    df = util.df(bars)
    if df is None or df.empty:
        return pd.DataFrame()
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    keep = [c for c in ("open", "high", "low", "close", "volume") if c in df.columns]
    return df[keep]


def _cache_path(symbol: str, bar_size: str):
    slug = bar_size.replace(" ", "")
    return config.BARS_DIR / f"{symbol.upper()}_{slug}.parquet"


def download_bars(
    ib: IB,
    symbol: str,
    duration: str = "5 Y",
    bar_size: str = "1 day",
    what_to_show: str = "ADJUSTED_LAST",
    use_rth: bool = True,
    save: bool = True,
) -> pd.DataFrame:
    """
    拉一个标的的历史K线。返回 index=date 的 OHLCV DataFrame。

    what_to_show:
        ADJUSTED_LAST  复权成交价（做股票/ETF 长期回测用这个）
        TRADES         未复权成交价
        MIDPOINT       买卖中价（外汇、以及没有成交量的品种用）
    """
    contract = make_contract(symbol)
    # qualifyContracts 返回的列表长度永远等于入参个数，认不出来的位置放 None，
    # 所以 `if not ib.qualifyContracts(c)` 判断的是 `not [None]` == False，永远不触发。
    # 得查 conId（成功时 qualifyContracts 会就地把它写回 contract）。
    ib.qualifyContracts(contract)
    if not contract.conId:
        raise RuntimeError(f"{symbol}: 合约无法识别。检查代码拼写，或指定 primaryExchange。")

    frames: list[pd.DataFrame] = []
    max_chunk = _MAX_CHUNK.get(bar_size, "1 M")
    need_chunking = _duration_days(duration) > _duration_days(max_chunk)

    if what_to_show == "ADJUSTED_LAST" and need_chunking:
        # 复权数据不支持指定 endDateTime，只能一把梭。IBKR 通常也认，只是不保证。
        log.info("%s: 复权模式不支持分段，单次请求 %s", symbol, duration)
        need_chunking = False

    if not need_chunking:
        bars = ib.reqHistoricalData(
            contract, endDateTime="", durationStr=duration, barSizeSetting=bar_size,
            whatToShow=what_to_show, useRTH=use_rth, formatDate=1,
        )
        frames.append(_bars_to_df(bars))
        time.sleep(PACING_SLEEP)
    else:
        end = datetime.now()
        remaining = _duration_days(duration)
        chunk_days = _duration_days(max_chunk)
        while remaining > 0:
            bars = ib.reqHistoricalData(
                contract, endDateTime=end, durationStr=max_chunk, barSizeSetting=bar_size,
                whatToShow=what_to_show, useRTH=use_rth, formatDate=1,
            )
            time.sleep(PACING_SLEEP)
            chunk = _bars_to_df(bars)
            if chunk.empty:
                log.warning("%s: 在 %s 之前没有更多数据了，提前停止", symbol, end.date())
                break
            frames.append(chunk)
            end = chunk.index[0].to_pydatetime() - timedelta(seconds=1)
            remaining -= chunk_days

    df = pd.concat(frames) if frames else pd.DataFrame()
    if df.empty:
        raise RuntimeError(
            f"{symbol}: 没拿到任何数据。常见原因：\n"
            f"  - 没有该品种的历史数据权限（错误 162）\n"
            f"  - 请求太密触发限速，等 10 分钟再试\n"
            f"  - what_to_show={what_to_show!r} 不适用于该品种"
        )

    df = df[~df.index.duplicated(keep="last")].sort_index()
    log.info("%s: %d 根 %s，%s ~ %s", symbol, len(df), bar_size,
             df.index[0].date(), df.index[-1].date())

    if save:
        path = _cache_path(symbol, bar_size)
        if path.exists():  # 增量合并，不覆盖历史
            old = pd.read_parquet(path)
            df = pd.concat([old, df])
            df = df[~df.index.duplicated(keep="last")].sort_index()
        df.to_parquet(path)
        log.debug("已缓存 -> %s", path.name)

    return df


def download_universe(ib: IB, symbols: list[str], **kw) -> dict[str, pd.DataFrame]:
    """批量下载。失败的标的记下来继续，不要因为一个代码写错就全盘重来。"""
    if len(symbols) > 50:
        log.warning("一次下 %d 个标的，接近 10 分钟 60 次的限速上限，可能被拒。", len(symbols))

    out, failed = {}, []
    for i, s in enumerate(symbols, 1):
        try:
            log.info("[%d/%d] %s", i, len(symbols), s)
            out[s] = download_bars(ib, s, **kw)
        except Exception as e:  # noqa: BLE001
            log.error("%s 下载失败: %s", s, e)
            failed.append(s)
    if failed:
        log.warning("以下标的失败: %s", ", ".join(failed))
    return out


# ---------------- 读缓存（离线，不需要连 IBKR） ----------------

def cached_symbols(bar_size: str = "1 day") -> list[str]:
    slug = bar_size.replace(" ", "")
    return sorted(p.stem.rsplit("_", 1)[0] for p in config.BARS_DIR.glob(f"*_{slug}.parquet"))


def load_bars(symbol: str, bar_size: str = "1 day") -> pd.DataFrame:
    path = _cache_path(symbol, bar_size)
    if not path.exists():
        raise FileNotFoundError(
            f"{symbol} 没有本地缓存（{path.name}）。先跑 scripts/02_download_data.py"
        )
    return pd.read_parquet(path)


def load_universe_prices(symbols: list[str] | None = None, bar_size: str = "1 day",
                         field: str = "close") -> pd.DataFrame:
    """
    拼成回测用的宽表：index=日期, columns=标的, values=收盘价。

    只保留所有标的都有数据的日期区间（inner join）。
    这很重要：不同标的上市时间不同，用 outer join 会让早期只有一两个标的，
    回测出来的"策略"其实是在赌那一两个品种。
    """
    symbols = symbols or cached_symbols(bar_size)
    if not symbols:
        raise FileNotFoundError("本地一个缓存都没有。先跑 scripts/02_download_data.py")

    series = {}
    for s in symbols:
        try:
            series[s] = load_bars(s, bar_size)[field]
        except FileNotFoundError as e:
            log.warning("%s", e)

    if not series:
        raise FileNotFoundError("指定的标的都没有缓存。")

    wide = pd.DataFrame(series).sort_index()
    before = len(wide)
    wide = wide.dropna(how="any")
    if len(wide) < before:
        log.info("对齐后保留 %d/%d 行（%s 起，所有标的都有数据）",
                 len(wide), before, wide.index[0].date() if len(wide) else "N/A")
    return wide
