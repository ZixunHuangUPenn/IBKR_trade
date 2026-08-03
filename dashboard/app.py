"""
可视化平台。

    streamlit run dashboard/app.py

设计上的一个决定：dashboard 默认不连 IBKR，只读磁盘上的缓存和快照。
理由是 dashboard 会因为你随便点一下就整个重跑，而 broker 连接是有状态的、
有 clientId 限制的、断开重连有代价的 —— 两者的生命周期根本不匹配。
真需要刷新时，点按钮开一条一次性的短连接（跑在独立线程里），用完就断。

这不是偷懒，是正确的架构：看盘的东西挂了不能影响交易的东西。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd
import streamlit as st

import config
from backtest.engine import buy_and_hold, run_backtest
from backtest.metrics import compare, format_metrics
from dashboard import charts
from ibkr import account as acct
from ibkr import market_data as md
from strategies import REGISTRY

st.set_page_config(page_title="IBKR 量化工作台", page_icon="📈", layout="wide")


# ---------------- 数据加载（带缓存） ----------------

@st.cache_data(ttl=300, show_spinner=False)
def _load_prices(symbols: tuple[str, ...], bar_size: str) -> pd.DataFrame:
    return md.load_universe_prices(list(symbols), bar_size=bar_size)


@st.cache_data(ttl=300, show_spinner=False)
def _load_one(symbol: str, bar_size: str) -> pd.DataFrame:
    return md.load_bars(symbol, bar_size)


def _cached(bar_size: str = "1 day") -> list[str]:
    return md.cached_symbols(bar_size)


# ---------------- 侧边栏 ----------------

env = "🔴 实盘" if config.is_live_port() else "🟢 Paper"
st.sidebar.title("IBKR 量化工作台")
st.sidebar.caption(f"{env} · {config.HOST}:{config.PORT} · "
                   f"{'只读' if config.READONLY else '可下单'}")
if config.is_live_port() and not config.READONLY:
    st.sidebar.error("实盘 + 可下单模式")

if st.sidebar.button("清空数据缓存"):
    st.cache_data.clear()
    st.rerun()

st.sidebar.divider()
st.sidebar.caption(
    f"行情缓存: {len(_cached())} 个标的\n\n"
    f"目录: `{config.BARS_DIR.name}/`"
)


tab_acct, tab_mkt, tab_bt, tab_data = st.tabs(
    ["账户", "行情", "回测实验室", "数据管理"])


# ================= 账户 =================
with tab_acct:
    col_a, col_b = st.columns([3, 1])
    col_a.subheader("账户总览")

    if col_b.button("从 IBKR 刷新", width="stretch"):
        with st.spinner("连接中…"):
            try:
                from ibkr.connection import run_isolated
                run_isolated(lambda ib: acct.save_snapshot(ib))
                st.cache_data.clear()
                st.success("快照已更新")
            except Exception as e:  # noqa: BLE001
                st.error(f"刷新失败: {e}")

    snap = acct.load_snapshot()
    if not snap:
        st.info(
            "还没有账户快照。\n\n"
            "点右上角「从 IBKR 刷新」，或在终端跑 "
            "`python scripts/01_test_connection.py`。"
        )
    else:
        st.caption(f"快照时间 {snap['timestamp']} · 环境 {snap['env']}")
        s = snap["summary"]
        cols = st.columns(4)
        cols[0].metric("净清算值", f"${s.get('NetLiquidation', 0):,.0f}")
        cols[1].metric("现金", f"${s.get('TotalCashValue', 0):,.0f}")
        cols[2].metric("持仓市值", f"${s.get('GrossPositionValue', 0):,.0f}")
        cols[3].metric("浮动盈亏", f"${s.get('UnrealizedPnL', 0):,.0f}",
                       delta=f"{s.get('UnrealizedPnL', 0):,.0f}")

        pos = pd.DataFrame(snap["positions"])
        if pos.empty:
            st.info("当前无持仓。")
        else:
            left, right = st.columns([3, 2])
            left.dataframe(
                pos, hide_index=True,
                column_config={
                    "weight": st.column_config.NumberColumn("权重", format="%.1f%%"),
                    "market_value": st.column_config.NumberColumn("市值", format="$%.0f"),
                    "unrealized_pnl": st.column_config.NumberColumn("浮盈", format="$%.0f"),
                    "market_price": st.column_config.NumberColumn("现价", format="%.2f"),
                    "avg_cost": st.column_config.NumberColumn("成本", format="%.2f"),
                },
            )
            right.plotly_chart(charts.positions_pie(pos))

        eq = acct.load_equity_curve()
        if len(eq) > 1:
            st.plotly_chart(charts.equity_chart(
                {"账户净值": eq.set_index("timestamp")["net_liquidation"]},
                normalize=False))
        else:
            st.caption("多跑几次快照，这里会攒出你自己的账户净值曲线。")


# ================= 行情 =================
with tab_mkt:
    st.subheader("行情浏览")
    syms = _cached()
    if not syms:
        st.info("本地还没有行情缓存。先跑 `python scripts/02_download_data.py`。")
    else:
        c1, c2 = st.columns([1, 3])
        sym = c1.selectbox("标的", syms)
        ma_input = c2.text_input("均线（逗号分隔）", "20, 60, 200")
        try:
            mas = tuple(int(x) for x in ma_input.split(",") if x.strip())
        except ValueError:
            mas = (20, 60)
            st.warning("均线参数解析失败，用默认值 20/60。")

        df = _load_one(sym, "1 day")
        n = st.slider("显示最近多少个交易日", 60, len(df), min(500, len(df)), 10)
        view = df.tail(n)

        st.plotly_chart(charts.candlestick(view, sym, mas))

        r = view["close"].pct_change(fill_method=None).dropna()
        m = st.columns(5)
        m[0].metric("区间涨跌", f"{view['close'].iloc[-1] / view['close'].iloc[0] - 1:.2%}")
        m[1].metric("年化波动", f"{r.std() * (252 ** 0.5):.2%}")
        m[2].metric("最大回撤", f"{(view['close'] / view['close'].cummax() - 1).min():.2%}")
        m[3].metric("最新价", f"{view['close'].iloc[-1]:,.2f}")
        m[4].metric("数据起点", str(df.index[0].date()))


# ================= 回测实验室 =================
with tab_bt:
    st.subheader("回测实验室")
    syms = _cached()
    if len(syms) < 1:
        st.info("先下载行情数据：`python scripts/02_download_data.py`")
    else:
        left, right = st.columns([1, 3])

        with left:
            universe = st.multiselect(
                "标的池", syms, default=syms[:6],
                help="横截面策略需要多个标的才有意义")
            strat_name = st.selectbox("策略", list(REGISTRY))
            cls = REGISTRY[strat_name]
            st.caption(cls.description)

            params = {}
            for p in cls.params_spec:
                if p.kind == "int":
                    params[p.name] = st.slider(
                        p.label, int(p.min), int(p.max), int(p.default), int(p.step))
                else:
                    params[p.name] = st.slider(
                        p.label, float(p.min), float(p.max),
                        float(p.default), float(p.step))

            st.divider()
            st.caption("执行假设")
            comm = st.slider("手续费(bps,单边)", 0.0, 20.0, config.COMMISSION_BPS, 0.5)
            slip = st.slider("滑点(bps,单边)", 0.0, 50.0, config.SLIPPAGE_BPS, 0.5)
            band = st.slider("不调仓缓冲区", 0.0, 0.3, 0.0, 0.01,
                             help="总偏离小于此值就不动手。调大能显著降低换手。")
            lag = st.selectbox("信号延迟(交易日)", [1, 0, 2], index=0,
                               help="1 = 今收盘算信号、明天持有。0 = 故意的未来函数，仅供对照。")
            if lag == 0:
                st.warning("lag=0 含未来函数，结果不可信，只用来做对照。")

            log_scale = st.checkbox("对数坐标", value=True)
            run = st.button("运行回测", type="primary", width="stretch")

        with right:
            if not universe:
                st.info("左侧至少选一个标的。")
            elif run:
                try:
                    prices = _load_prices(tuple(universe), "1 day")
                except Exception as e:  # noqa: BLE001
                    st.error(f"加载数据失败: {e}")
                    st.stop()

                kw = dict(commission_bps=comm, slippage_bps=slip,
                          rebalance_band=band, lag=lag)
                strat = cls(**params)
                res = strat.backtest(prices, **kw)
                bench = buy_and_hold(prices, **{k: v for k, v in kw.items()
                                                if k != "rebalance_band"})

                curves = {res.name: res.equity, bench.name: bench.equity}

                cols = st.columns(5)
                mm = res.metrics
                cols[0].metric("年化收益", f"{mm['cagr']:.2%}",
                               delta=f"{mm['cagr'] - bench.metrics['cagr']:+.2%} vs 基准")
                cols[1].metric("夏普", f"{mm['sharpe']:.2f}")
                cols[2].metric("最大回撤", f"{mm['max_drawdown']:.2%}")
                cols[3].metric("年化波动", f"{mm['ann_vol']:.2%}")
                cols[4].metric("年换手", f"{mm['ann_turnover']:.0%}")

                st.plotly_chart(charts.equity_chart(curves, log_scale=log_scale))
                st.plotly_chart(charts.drawdown_chart(curves))
                st.plotly_chart(charts.weights_area(res.held_weights))
                st.plotly_chart(charts.rolling_chart(res.returns))

                with st.expander("完整指标对比"):
                    st.dataframe(
                        compare({res.name: res.metrics, bench.name: bench.metrics}),
                        width="stretch")

                with st.expander("最新目标权重（今天收盘后该持有的仓位）"):
                    latest = pd.Series(strat.latest_weights(prices), name="目标权重")
                    latest = latest[latest.abs() > 1e-9].sort_values(ascending=False)
                    if latest.empty:
                        st.write("策略当前信号为空仓。")
                    else:
                        st.dataframe(latest.to_frame().style.format("{:.2%}"))
                    st.caption("把这组权重交给 `scripts/04_paper_trade.py` 就能生成订单。")
            else:
                st.info("设好参数，点「运行回测」。")


# ================= 数据管理 =================
with tab_data:
    st.subheader("数据管理")
    rows = []
    for p in sorted(config.BARS_DIR.glob("*.parquet")):
        try:
            df = pd.read_parquet(p)
            rows.append({
                "文件": p.name,
                "标的": p.stem.rsplit("_", 1)[0],
                "周期": p.stem.rsplit("_", 1)[1],
                "行数": len(df),
                "起始": str(df.index[0].date()),
                "结束": str(df.index[-1].date()),
                "大小KB": round(p.stat().st_size / 1024, 1),
            })
        except Exception as e:  # noqa: BLE001
            rows.append({"文件": p.name, "标的": "读取失败", "周期": str(e)})

    if rows:
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    else:
        st.info("还没有缓存任何行情数据。")

    st.divider()
    st.markdown(
        "**下载新数据**（在终端跑，不在这里跑）：\n\n"
        "```\n"
        "python scripts/02_download_data.py --symbols SPY,QQQ,TLT --duration 10 Y\n"
        "```\n\n"
        "为什么不做成按钮：IBKR 历史数据有 10 分钟 60 次的限速，"
        "点错一下就要等十分钟。放在终端里，你会更清楚自己请求了什么。"
    )
