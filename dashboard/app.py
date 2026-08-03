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
from dashboard import charts, labels, portfolios, strategy_guide
from ibkr import account as acct
from ibkr import market_data as md
from strategies import REGISTRY, FixedWeights

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

# 代码对照表放侧边栏：任何一个 tab 看到不认识的代码，抬眼就能查，
# 不用退出当前操作
with st.sidebar.expander("📖 标的代码对照"):
    _syms = _cached()
    if not _syms:
        st.caption("还没有缓存数据。")
    else:
        _real = [s for s in _syms if not labels.is_synthetic(s)]
        _synth = [s for s in _syms if labels.is_synthetic(s)]
        for _s in _real:
            st.markdown(f"**{_s}** — {labels.name(_s)}  \n"
                        f"<span style='font-size:0.8em;opacity:0.7'>"
                        f"{labels.asset_class(_s)}</span>",
                        unsafe_allow_html=True)
        if _synth:
            st.caption("⚠️ 以下为合成数据，非真实行情：")
            for _s in _synth:
                st.markdown(f"**{_s}** — {labels.name(_s)}")


tab_acct, tab_pf, tab_mkt, tab_bt, tab_guide, tab_data = st.tabs(
    ["账户", "持仓方案", "行情", "回测实验室", "策略介绍", "数据管理"])


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
            # 在代码后面插一列中文名，扫一眼就知道持仓偏向哪类资产
            if "symbol" in pos.columns:
                pos.insert(1, "中文名", pos["symbol"].map(labels.name))
            left, right = st.columns([3, 2])
            left.dataframe(
                pos, hide_index=True,
                column_config={
                    "symbol": st.column_config.TextColumn("代码"),
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


# ================= 持仓方案 =================
# 这一页回答的是"钱该怎么分"，不是"什么时候买卖"。
# 放在账户后面、行情前面，是因为决策顺序本来就该是这个：
# 先定配置，再看行情，最后才轮到调策略参数。
with tab_pf:
    st.subheader("持仓方案")
    st.caption("绝大多数人的长期收益来自「配了什么」，而不是「什么时候买卖」。"
               "先把这一页想清楚，再去回测实验室调参数。")

    pf_left, pf_right = st.columns([1, 2])

    with pf_left:
        preset_name = st.selectbox("配置方案", list(portfolios.PRESETS))
        pf = portfolios.PRESETS[preset_name]
        st.caption(pf.tagline)

        custom = st.checkbox("自定义权重", value=False,
                             help="勾上后可以在下方直接改数字，方案模板作为起点")

        weights = dict(pf.weights)
        if custom:
            edited = st.data_editor(
                pd.DataFrame({"标的": list(weights), "权重%":
                              [round(v * 100, 2) for v in weights.values()]}),
                num_rows="dynamic", hide_index=True, width="stretch",
                column_config={
                    "权重%": st.column_config.NumberColumn(
                        min_value=0.0, max_value=100.0, step=1.0, format="%.2f"),
                },
                key="pf_editor",
            )
            weights = {
                str(r["标的"]).strip().upper(): float(r["权重%"]) / 100.0
                for _, r in edited.iterrows()
                if str(r["标的"]).strip() and pd.notna(r["权重%"])
            }

        total = sum(weights.values())
        if abs(total - 1.0) > 1e-6:
            # 不自动归一化：权重和不是 1 通常意味着写错了，
            # 悄悄帮你改掉反而掩盖问题。给按钮，让你自己决定。
            st.warning(f"权重合计 {total:.2%}，不等于 100%。"
                       f"{'剩余部分算作现金。' if total < 1 else '这是加杠杆。'}")

        st.divider()
        st.markdown(f"**风险等级**　{portfolios.RISK_LABEL[pf.risk_level]} "
                    f"（{'●' * pf.risk_level}{'○' * (5 - pf.risk_level)}）")
        st.caption(f"出处：{pf.source}")

    with pf_right:
        if not weights:
            st.info("先填至少一个标的和权重。")
        else:
            pie_l, pie_r = st.columns(2)
            pie_l.plotly_chart(charts.weight_pie(
                weights, title="按标的", name_fn=labels.name,
                center=f"{len(weights)} 个标的<br>合计 {total:.0%}"))
            pie_r.plotly_chart(charts.weight_pie(
                portfolios.by_asset_class(weights, labels.asset_class),
                title="按资产类别",
                center="风险来源<br>看这张图"))
            st.caption("两张饼一起看：左边告诉你买了什么，右边告诉你风险来自哪里。"
                       "很多「看起来很分散」的组合，右图会露馅。")

    st.divider()
    md_l, md_r = st.columns(2)
    md_l.markdown(f"**为什么这么配**\n\n{pf.idea}")
    md_r.markdown(f"**适合谁**\n\n{pf.suits}\n\n"
                  f"**⚠️ 最该提防的**\n\n{pf.watch}")

    # ---- 和实际持仓对比 ----
    st.divider()
    st.markdown("#### 与当前账户对比")
    snap_pf = acct.load_snapshot()
    actual: dict[str, float] = {}
    nav_pf = 0.0
    if snap_pf:
        nav_pf = float(snap_pf["summary"].get("NetLiquidation", 0) or 0)
        for p in snap_pf["positions"]:
            if nav_pf:
                actual[p["symbol"]] = float(p["market_value"]) / nav_pf

    if not snap_pf:
        st.info("还没有账户快照，无法对比。去「账户」页点「从 IBKR 刷新」。")
    elif not actual:
        st.info(f"账户当前空仓（净值 ${nav_pf:,.0f}）。下面是按这个方案建仓需要买的量。")
    else:
        st.plotly_chart(charts.target_vs_actual(weights, actual, name_fn=labels.name))

    if snap_pf and nav_pf > 0:
        # 用本地缓存的收盘价估算股数。刻意不连 IBKR 取实时价 ——
        # 这一页是拿来想清楚配置的，不是拿来下单的；真下单走 04_paper_trade.py。
        px_rows = []
        for sym in sorted(set(weights) | set(actual)):
            tgt_w = weights.get(sym, 0.0)
            cur_w = actual.get(sym, 0.0)
            try:
                px = float(_load_one(sym, "1 day")["close"].iloc[-1])
            except Exception:  # noqa: BLE001
                px = float("nan")
            delta_val = (tgt_w - cur_w) * nav_pf
            px_rows.append({
                "标的": sym,
                "中文名": labels.name(sym),
                "目标%": tgt_w * 100,
                "当前%": cur_w * 100,
                "偏离pp": (cur_w - tgt_w) * 100,
                "需调整$": delta_val,
                "约股数": (delta_val / px) if px == px and px > 0 else float("nan"),
                "参考价": px,
            })
        st.dataframe(
            pd.DataFrame(px_rows), hide_index=True, width="stretch",
            column_config={
                "目标%": st.column_config.NumberColumn(format="%.2f%%"),
                "当前%": st.column_config.NumberColumn(format="%.2f%%"),
                "偏离pp": st.column_config.NumberColumn(format="%+.2f"),
                "需调整$": st.column_config.NumberColumn(format="$%.0f"),
                "约股数": st.column_config.NumberColumn(format="%.0f"),
                "参考价": st.column_config.NumberColumn(format="%.2f"),
            },
        )
        st.caption(
            f"净值 ${nav_pf:,.0f}，股数按**本地缓存的最后一根收盘价**估算，仅供判断量级。"
            "正数=买入，负数=卖出。真要下单请走 "
            "`python scripts/04_paper_trade.py`，它会取实时价、算整股、做体检。"
        )

    # ---- 回测这个方案 ----
    st.divider()
    st.markdown("#### 这个方案历史上表现如何")
    miss = portfolios.missing_symbols(weights, _cached())
    if miss:
        st.warning(f"这些标的没有本地缓存，无法回测：{', '.join(miss)}。"
                   f"先跑 `python scripts/02_download_data.py --symbols {','.join(miss)}`")
    elif not weights:
        st.info("先设置权重。")
    else:
        bt_c1, bt_c2, bt_c3 = st.columns([1, 1, 2])
        months = bt_c1.selectbox("调仓间隔(月)", [3, 1, 6, 12], index=0)
        pf_log = bt_c2.checkbox("对数坐标", value=True, key="pf_log")
        if bt_c3.button("回测该方案", type="primary", width="stretch"):
            try:
                pf_prices = _load_prices(tuple(sorted(weights)), "1 day")
                pf_strat = FixedWeights(weights=weights, rebalance_months=int(months))
                pf_res = pf_strat.backtest(pf_prices)
                pf_bench = buy_and_hold(pf_prices)
            except Exception as e:  # noqa: BLE001
                st.error(f"回测失败: {e}")
            else:
                mm = pf_res.metrics
                mc = st.columns(5)
                mc[0].metric("年化收益", f"{mm['cagr']:.2%}",
                             delta=f"{mm['cagr'] - pf_bench.metrics['cagr']:+.2%} vs 等权持有")
                mc[1].metric("夏普", f"{mm['sharpe']:.2f}")
                mc[2].metric("最大回撤", f"{mm['max_drawdown']:.2%}")
                mc[3].metric("年化波动", f"{mm['ann_vol']:.2%}")
                mc[4].metric("年换手", f"{mm['ann_turnover']:.0%}")
                pf_label = f"{preset_name}（已改）" if custom else preset_name
                st.plotly_chart(charts.equity_chart(
                    {pf_label: pf_res.equity, pf_bench.name: pf_bench.equity},
                    log_scale=pf_log))
                st.plotly_chart(charts.drawdown_chart({pf_label: pf_res.equity}))
                st.caption(
                    f"区间 {pf_prices.index[0].date()} ~ {pf_prices.index[-1].date()}"
                    f"（{len(pf_prices)} 个交易日）。"
                    "只覆盖所有标的都有数据的那一段 —— 有的 ETF 上市晚，"
                    "区间会被最年轻的那个标的截短。"
                )


# ================= 行情 =================
with tab_mkt:
    st.subheader("行情浏览")
    syms = _cached()
    if not syms:
        st.info("本地还没有行情缓存。先跑 `python scripts/02_download_data.py`。")
    else:
        c1, c2 = st.columns([1, 3])
        sym = c1.selectbox("标的", syms, format_func=labels.label)
        ma_input = c2.text_input("均线（逗号分隔）", "20, 60, 200")

        if labels.is_synthetic(sym):
            st.warning(f"**{sym} · {labels.name(sym)}** {labels.detail(sym)}")
        else:
            st.caption(f"**{labels.name(sym)}**（{labels.asset_class(sym)}）"
                       f" · {labels.detail(sym)}")
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
                format_func=labels.label,
                help="横截面策略需要多个标的才有意义")
            if any(labels.is_synthetic(s) for s in universe):
                st.warning("标的池里含 SYNTH_ 合成数据，回测结果只用于验证代码链路，"
                           "不代表任何真实策略表现。")
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
                        # 带上中文名：光看 "EFA 32%" 判断不了这组仓位偏向哪类资产
                        st.dataframe(
                            pd.DataFrame({
                                "代码": latest.index,
                                "中文名": [labels.name(s) for s in latest.index],
                                "类别": [labels.asset_class(s) for s in latest.index],
                                "目标权重": latest.values * 100,  # 列格式按百分数显示
                            }),
                            hide_index=True, width="stretch",
                            column_config={
                                "目标权重": st.column_config.NumberColumn(format="%.2f%%"),
                            },
                        )
                    st.caption("把这组权重交给 `scripts/04_paper_trade.py` 就能生成订单。")
            else:
                st.info("设好参数，点「运行回测」。")


# ================= 策略介绍 =================
# 只讲优点的策略介绍等于广告。这里「劣势」和「什么时候别用」写得和优点一样细，
# 因为策略选错的代价比参数调错大一个量级。
with tab_guide:
    st.subheader("策略介绍")
    st.caption("下拉框里那四个名字背后各自是什么、什么时候该用、什么时候会失效。"
               "每条结论都能在「回测实验室」里自己验证。")

    g_pick, g_cmp, g_pit = st.tabs(["逐个详解", "横向对比 & 怎么选", "通用陷阱"])

    # ---- 逐个详解 ----
    with g_pick:
        names = [g.key for g in strategy_guide.GUIDES]
        picked = st.radio("选一个策略", names, horizontal=True, label_visibility="collapsed")
        g = strategy_guide.get(picked)

        st.markdown(f"### {g.key}")
        st.markdown(f"**{g.tagline}**")
        st.caption(f"策略族：{g.family}")

        # 画像放最前面：不想读长文的人，看这一行就够做初筛。
        # metric 的宽度装不下长文本会截断，全文和维度解释挂在 ⓘ tooltip 里。
        prof = st.columns(len(g.profile))
        for col, (k, v) in zip(prof, g.profile.items()):
            col.metric(k, v, help=f"**{v}**\n\n{strategy_guide.PROFILE_HELP.get(k, '')}")

        # 先给结论（能不能用、什么时候会坏），再讲机制。
        # 读者的第一个问题是"这个适不适合我"，不是"它怎么算的"。
        st.divider()
        pro_col, con_col = st.columns(2)
        with pro_col:
            st.markdown("#### ✅ 优势")
            for x in g.pros:
                st.markdown(f"- {x}")
        with con_col:
            st.markdown("#### ⚠️ 劣势")
            for x in g.cons:
                st.markdown(f"- {x}")

        use_col, avoid_col = st.columns(2)
        with use_col:
            st.markdown("#### 什么时候用")
            for x in g.use_when:
                st.markdown(f"- {x}")
        with avoid_col:
            st.markdown("#### 什么时候别用")
            for x in g.avoid_when:
                st.markdown(f"- {x}")

        st.error(f"**典型失效场景**　{g.failure}")

        st.divider()
        st.markdown("#### 核心逻辑")
        st.markdown(g.idea)
        st.markdown("**信号怎么算**")
        st.code(g.formula, language="text")

        st.divider()
        st.markdown("#### 参数解析")
        st.dataframe(
            pd.DataFrame([{"参数": p[0], "含义": p[1], "调整方向的后果": p[2]}
                          for p in g.params]),
            hide_index=True, width="stretch",
        )

        st.divider()
        st.markdown("#### 代码模板")
        st.code(g.template, language="python")
        st.markdown("**命令行等价写法**")
        st.code(g.cli, language="bash")
        st.caption("想交互式调参就用「回测实验室」那一页；"
                   "要复现、要存结果、要进定时任务，就用命令行。")

    # ---- 横向对比 ----
    with g_cmp:
        st.markdown("#### 四个策略横向对比")
        st.dataframe(pd.DataFrame(strategy_guide.comparison_table()),
                     hide_index=True, width="stretch")
        st.caption("表里没有「收益」这一列 —— 收益取决于标的池和区间，"
                   "任何写死的数字都是误导。想知道谁强，去回测实验室用你自己的池子跑一遍。")
        st.markdown(strategy_guide.DECISION_GUIDE)

    # ---- 通用陷阱 ----
    with g_pit:
        st.markdown("#### 与策略无关、但更致命的七件事")
        st.caption("下面每一条都能让一个「年化 30%」的回测在实盘里变成亏损。"
                   "选策略之前先把这些排掉。")
        for i, (title, what, how) in enumerate(strategy_guide.PITFALLS, 1):
            with st.expander(f"{i}. {title}"):
                st.markdown(f"**是什么**　{what}")
                st.markdown(f"**怎么办**　{how}")


# ================= 数据管理 =================
with tab_data:
    st.subheader("数据管理")
    rows = []
    for p in sorted(config.BARS_DIR.glob("*.parquet")):
        try:
            df = pd.read_parquet(p)
            sym = p.stem.rsplit("_", 1)[0]
            rows.append({
                "文件": p.name,
                "标的": sym,
                "中文名": labels.name(sym),
                "类别": labels.asset_class(sym),
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
        "（SPY = 标普500，QQQ = 纳斯达克100，TLT = 美国长期国债；"
        "更多代码的中文对照见左侧边栏「标的代码对照」，"
        "新增代码可在 `dashboard/labels.py` 里登记。）\n\n"
        "为什么不做成按钮：IBKR 历史数据有 10 分钟 60 次的限速，"
        "点错一下就要等十分钟。放在终端里，你会更清楚自己请求了什么。"
    )
