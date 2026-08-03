"""
标的代码 -> 中文注释。

纯查表，不依赖任何外部数据源 —— dashboard 上写着 "EFA"、"DBC" 这种代码，
不查一下根本不知道自己在回测什么。看错资产类别是很贵的错误：
把 TLT（长债）当成 IEF（中债）选进标的池，久期差了一倍多，
回测出来的波动和回撤完全不是一回事。

查不到的代码不报错、不猜，原样显示代码 —— 宁可没注释，也不能给个错注释。
"""

from __future__ import annotations

from typing import NamedTuple


class SymbolInfo(NamedTuple):
    name: str    # 中文简称，短，塞得进下拉框
    asset: str   # 资产类别，用来分组
    detail: str  # 一句话说明：跟踪什么指数、有什么特点


# 顺序即 config.DEFAULT_UNIVERSE 的顺序：股票（美国大→小→国际→新兴）、
# 债券（长→中）、商品、房地产。这是一个标准的多资产配置池。
SYMBOLS: dict[str, SymbolInfo] = {
    # ---- 股票 ----
    "SPY": SymbolInfo("标普500", "美股", "SPDR S&P 500 ETF，跟踪标普500指数，美国大盘股，全球流动性最好的 ETF"),
    "QQQ": SymbolInfo("纳斯达克100", "美股", "Invesco QQQ，跟踪纳斯达克100指数，科技成长股为主，波动明显高于 SPY"),
    "IWM": SymbolInfo("罗素2000", "美股", "iShares Russell 2000 ETF，美国小盘股，经济周期敏感、波动大"),
    "VTI": SymbolInfo("美股全市场", "美股", "Vanguard Total Stock Market ETF，覆盖美国大中小盘，和 SPY 高度重叠"),
    "DIA": SymbolInfo("道琼斯30", "美股", "SPDR Dow Jones Industrial Average ETF，30 只价格加权的蓝筹股"),
    "EFA": SymbolInfo("发达市场(除美)", "国际股票", "iShares MSCI EAFE ETF，欧洲/澳洲/远东发达市场，不含美国和加拿大"),
    "VEA": SymbolInfo("发达市场(除美)", "国际股票", "Vanguard FTSE Developed Markets ETF，和 EFA 定位相同、费率更低"),
    "EEM": SymbolInfo("新兴市场", "国际股票", "iShares MSCI Emerging Markets ETF，中国/印度/巴西等，权重偏中国"),
    "VWO": SymbolInfo("新兴市场", "国际股票", "Vanguard FTSE Emerging Markets ETF，和 EEM 定位相同、费率更低"),

    # ---- 债券 ----
    "TLT": SymbolInfo("美国长期国债", "债券", "iShares 20+ Year Treasury Bond ETF，20年期以上，久期约17年，对利率极度敏感"),
    "IEF": SymbolInfo("美国中期国债", "债券", "iShares 7-10 Year Treasury Bond ETF，久期约7年，波动约为 TLT 的一半"),
    "SHY": SymbolInfo("美国短期国债", "债券", "iShares 1-3 Year Treasury Bond ETF，久期不到2年，接近现金"),
    "AGG": SymbolInfo("美国综合债券", "债券", "iShares Core U.S. Aggregate Bond ETF，国债+机构债+投资级信用债"),
    "LQD": SymbolInfo("投资级公司债", "债券", "iShares iBoxx Investment Grade Corporate Bond ETF，含信用利差"),
    "HYG": SymbolInfo("高收益债", "债券", "iShares iBoxx High Yield Corporate Bond ETF，垃圾债，股票属性比债券强"),
    "TIP": SymbolInfo("通胀保值国债", "债券", "iShares TIPS Bond ETF，本金随 CPI 调整，对冲通胀"),

    # ---- 商品 / 另类 ----
    "GLD": SymbolInfo("黄金", "商品", "SPDR Gold Shares，持有实物黄金，无票息，危机时的避险资产"),
    "SLV": SymbolInfo("白银", "商品", "iShares Silver Trust，持有实物白银，工业属性使其波动大于黄金"),
    "DBC": SymbolInfo("大宗商品", "商品", "Invesco DB Commodity Index Tracking Fund，一篮子期货，能源权重最高"),
    "USO": SymbolInfo("原油", "商品", "United States Oil Fund，持有原油期货，有展期损耗，不适合长持"),

    # ---- 房地产 ----
    "VNQ": SymbolInfo("美国REITs", "房地产", "Vanguard Real Estate ETF，美国房地产信托，股债之间的第三类资产"),
}

# 合成数据（scripts/00_offline_demo.py --save-cache 生成）。
# 单独标出来是因为它们最危险 —— 格式和真实行情一模一样，
# 回测跑出来也有漂亮的净值曲线，唯独不能当真。
SYNTH_PREFIX = "SYNTH_"
SYNTH: dict[str, SymbolInfo] = {
    "STK_US": SymbolInfo("模拟-美股", "合成数据", "几何布朗运动模拟，设定年化收益 9%、波动 16%"),
    "STK_INTL": SymbolInfo("模拟-国际股票", "合成数据", "几何布朗运动模拟，设定年化收益 6%、波动 18%"),
    "STK_EM": SymbolInfo("模拟-新兴市场", "合成数据", "几何布朗运动模拟，设定年化收益 5%、波动 24%"),
    "BOND": SymbolInfo("模拟-债券", "合成数据", "几何布朗运动模拟，设定年化收益 2%、波动 6%，与市场因子负相关"),
    "GOLD": SymbolInfo("模拟-黄金", "合成数据", "几何布朗运动模拟，设定年化收益 4%、波动 15%"),
    "REIT": SymbolInfo("模拟-房地产", "合成数据", "几何布朗运动模拟，设定年化收益 7%、波动 22%"),
}


def info(symbol: str) -> SymbolInfo | None:
    """查不到返回 None，由调用方决定怎么降级显示。"""
    s = symbol.upper()
    if s.startswith(SYNTH_PREFIX):
        return SYNTH.get(s[len(SYNTH_PREFIX):])
    return SYMBOLS.get(s)


def is_synthetic(symbol: str) -> bool:
    return symbol.upper().startswith(SYNTH_PREFIX)


def label(symbol: str) -> str:
    """下拉框/多选框里的显示文本：`QQQ · 纳斯达克100`。查不到就只显示代码。"""
    i = info(symbol)
    return f"{symbol} · {i.name}" if i else symbol


def name(symbol: str) -> str:
    """只要中文简称，用于表格列。"""
    i = info(symbol)
    return i.name if i else "—"


def asset_class(symbol: str) -> str:
    i = info(symbol)
    return i.asset if i else "未分类"


def detail(symbol: str) -> str:
    """一句话说明，用于 caption。查不到时给一句明确的"不知道"，别装懂。"""
    i = info(symbol)
    if i:
        prefix = "⚠️ 合成数据，非真实行情 —— " if is_synthetic(symbol) else ""
        return f"{prefix}{i.detail}"
    return f"{symbol}：没有登记中文说明（可在 dashboard/labels.py 里补上）"


def legend(symbols: list[str]) -> list[dict[str, str]]:
    """给一组标的生成对照表，用于 dashboard 的图例区。"""
    return [
        {"代码": s, "中文名": name(s), "类别": asset_class(s),
         "说明": info(s).detail if info(s) else "未登记"}
        for s in symbols
    ]
