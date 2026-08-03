"""图表工具。统一配色和布局，让所有图看起来像一套东西。"""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go

# 定性配色：色相分散、明度接近，在浅色和深色背景下都能读
PALETTE = ["#4C78A8", "#F58518", "#54A24B", "#E45756", "#72B7B2",
           "#B279A2", "#FF9DA6", "#9D755D", "#EECA3B", "#BAB0AC"]

UP, DOWN = "#54A24B", "#E45756"


def _layout(fig: go.Figure, height: int = 380, **kw) -> go.Figure:
    fig.update_layout(
        height=height,
        margin=dict(l=10, r=10, t=36, b=10),
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        xaxis=dict(showgrid=False),
        yaxis=dict(gridcolor="rgba(128,128,128,0.18)", zeroline=False),
        **kw,
    )
    return fig


def equity_chart(curves: dict[str, pd.Series], log_scale: bool = False,
                 normalize: bool = True) -> go.Figure:
    """净值曲线对比。默认归一化到 1，这样不同起始资金也能直接比。"""
    fig = go.Figure()
    for i, (name, s) in enumerate(curves.items()):
        y = s / s.iloc[0] if normalize and len(s) and s.iloc[0] else s
        fig.add_trace(go.Scatter(
            x=y.index, y=y, name=name, mode="lines",
            line=dict(width=2, color=PALETTE[i % len(PALETTE)]),
            hovertemplate="%{y:.3f}<extra>" + name + "</extra>",
        ))
    _layout(fig, 420, title="净值曲线")
    if log_scale:
        # 长周期一定要看对数坐标：线性坐标下早期的翻倍看起来像条平线
        fig.update_yaxes(type="log")
    return fig


def drawdown_chart(curves: dict[str, pd.Series]) -> go.Figure:
    """回撤图。比净值曲线更能说明"你能不能拿得住"。"""
    fig = go.Figure()
    for i, (name, s) in enumerate(curves.items()):
        dd = s / s.cummax() - 1.0
        fig.add_trace(go.Scatter(
            x=dd.index, y=dd, name=name, mode="lines",
            line=dict(width=1.5, color=PALETTE[i % len(PALETTE)]),
            fill="tozeroy" if len(curves) == 1 else None,
            hovertemplate="%{y:.2%}<extra>" + name + "</extra>",
        ))
    fig = _layout(fig, 260, title="回撤")
    fig.update_yaxes(tickformat=".0%")
    return fig


def weights_area(weights: pd.DataFrame, max_series: int = 12) -> go.Figure:
    """持仓权重堆叠图。一眼看出策略到底在什么时候拿了什么。"""
    w = weights.loc[:, weights.abs().sum() > 0]
    if w.shape[1] > max_series:
        keep = w.abs().sum().nlargest(max_series).index
        w = w[keep]

    fig = go.Figure()
    for i, col in enumerate(w.columns):
        fig.add_trace(go.Scatter(
            x=w.index, y=w[col], name=str(col), mode="lines",
            stackgroup="one", line=dict(width=0, color=PALETTE[i % len(PALETTE)]),
            hovertemplate="%{y:.1%}<extra>" + str(col) + "</extra>",
        ))
    fig = _layout(fig, 300, title="持仓权重")
    fig.update_yaxes(tickformat=".0%")
    return fig


def candlestick(df: pd.DataFrame, symbol: str, mas: tuple[int, ...] = (20, 60)) -> go.Figure:
    fig = go.Figure()
    fig.add_trace(go.Candlestick(
        x=df.index, open=df["open"], high=df["high"], low=df["low"], close=df["close"],
        name=symbol, increasing_line_color=UP, decreasing_line_color=DOWN,
    ))
    for i, n in enumerate(mas):
        if len(df) >= n:
            fig.add_trace(go.Scatter(
                x=df.index, y=df["close"].rolling(n).mean(), name=f"MA{n}",
                line=dict(width=1.4, color=PALETTE[i % len(PALETTE)]),
            ))
    fig = _layout(fig, 460, title=f"{symbol} 日线")
    fig.update_layout(xaxis_rangeslider_visible=False)
    return fig


def positions_pie(df: pd.DataFrame) -> go.Figure:
    d = df[df["market_value"] > 0]
    fig = go.Figure(go.Pie(
        labels=d["symbol"], values=d["market_value"], hole=0.55,
        marker=dict(colors=PALETTE), textinfo="label+percent",
    ))
    return _layout(fig, 340, title="持仓分布")


def weight_pie(weights: dict[str, float], title: str = "目标配置",
               center: str = "", name_fn=None, height: int = 380) -> go.Figure:
    """
    权重饼图（环形）。按权重降序排，最大的一块从 12 点开始 ——
    顺序固定了，不同方案之间的配色才有可比性。

    name_fn: 标的 -> 中文名，传 labels.name 就能显示 "SPY 标普500"。
    """
    items = sorted(((k, v) for k, v in weights.items() if abs(v) > 1e-9),
                   key=lambda kv: -abs(kv[1]))
    if not items:
        fig = go.Figure()
        fig.add_annotation(text="空仓", showarrow=False, font=dict(size=16))
        return _layout(fig, height, title=title)

    keys = [k for k, _ in items]
    vals = [abs(v) for _, v in items]
    text = [f"{k}<br>{name_fn(k)}" if name_fn else k for k in keys]

    fig = go.Figure(go.Pie(
        labels=text, values=vals, hole=0.55, sort=False, direction="clockwise",
        marker=dict(colors=PALETTE, line=dict(color="rgba(128,128,128,0.25)", width=1)),
        textinfo="label+percent", textposition="auto",
        hovertemplate="%{label}<br>%{percent}<extra></extra>",
    ))
    if center:
        # 环心那块空白不用白不用：放总仓位或方案名，省一行文字说明
        fig.add_annotation(text=center, showarrow=False,
                           font=dict(size=13), align="center")
    fig = _layout(fig, height, title=title)
    fig.update_layout(hovermode=None, showlegend=False)
    return fig


def target_vs_actual(target: dict[str, float], actual: dict[str, float],
                     name_fn=None) -> go.Figure:
    """
    目标 vs 实际的分组横向柱状图。

    横向而非纵向：标的代码写在 y 轴上不会挤成一团，
    而且视线从上往下扫一遍就能找出偏离最大的那几个。
    """
    syms = sorted(set(target) | set(actual),
                  key=lambda s: -max(abs(target.get(s, 0)), abs(actual.get(s, 0))))
    labels_y = [f"{s} {name_fn(s)}" if name_fn else s for s in syms]

    fig = go.Figure()
    fig.add_trace(go.Bar(
        y=labels_y, x=[target.get(s, 0.0) for s in syms], name="目标权重",
        orientation="h", marker_color=PALETTE[0],
        hovertemplate="%{x:.2%}<extra>目标</extra>",
    ))
    fig.add_trace(go.Bar(
        y=labels_y, x=[actual.get(s, 0.0) for s in syms], name="实际权重",
        orientation="h", marker_color=PALETTE[1],
        hovertemplate="%{x:.2%}<extra>实际</extra>",
    ))
    fig = _layout(fig, max(260, 42 * len(syms) + 90), title="目标 vs 实际")
    fig.update_layout(barmode="group", hovermode="y unified",
                      yaxis=dict(autorange="reversed"))
    fig.update_xaxes(tickformat=".0%")
    return fig


def rolling_chart(returns: pd.Series, window: int = 252) -> go.Figure:
    """滚动年化收益和波动。看策略的表现是稳定的，还是全靠某一段行情。"""
    ann_ret = returns.rolling(window).mean() * 252
    ann_vol = returns.rolling(window).std() * (252 ** 0.5)

    fig = go.Figure()
    fig.add_trace(go.Scatter(x=ann_ret.index, y=ann_ret, name=f"滚动{window}日年化收益",
                             line=dict(width=1.8, color=PALETTE[0])))
    fig.add_trace(go.Scatter(x=ann_vol.index, y=ann_vol, name=f"滚动{window}日年化波动",
                             line=dict(width=1.8, color=PALETTE[1])))
    fig.add_hline(y=0, line=dict(width=1, color="rgba(128,128,128,0.5)"))
    fig = _layout(fig, 280, title="滚动表现")
    fig.update_yaxes(tickformat=".0%")
    return fig
