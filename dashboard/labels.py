"""
标的代码 -> 中文注释。

纯查表，不联网、不调 API —— dashboard 上写着 "EFA"、"DBC" 这种代码，
不查一下根本不知道自己在回测什么。看错资产类别是很贵的错误：
把 TLT（长债）当成 IEF（中债）选进标的池，久期差了一倍多，
回测出来的波动和回撤完全不是一回事。

查不到的代码不报错、不猜，原样显示代码 —— 宁可没注释，也不能给个错注释。

两张表，优先级从高到低：

  SYMBOLS       手写的。ETF 和少量个股，我自己核过，是权威。
  symbol_names  agent 每天 commit 时登记的。它可以提名任意美股代码，
                标的池不再是 config.DEFAULT_UNIVERSE 那十个 ETF 能框住的了 ——
                持仓表里蹦出一个 "GE" 却只显示 "—"，你得自己去搜它现在
                到底是什么公司（2024 年分拆后 GE 只剩航空发动机业务）。

为什么 agent 写的不直接进这个文件：这里的每一行都会被 import 成 Python 代码。
把模型生成的字符串拼进源码，等于给它开了一条在 dashboard 进程里执行任意代码的
路 —— 一个交易仓库不该有这种东西。落到 JSON 里读进来，最坏也只是显示一个
难看的名字。手写表永远盖过它，所以 agent 也改不了我核过的条目。
"""

from __future__ import annotations

import json
import logging
from typing import NamedTuple

import config

log = logging.getLogger("labels")

# agent 登记的名字。放在 AGENT_DIR 而不是源码目录，是因为它是数据不是代码：
# 删了不影响任何功能，只是名字变回代码而已。
LEARNED_FILE = config.AGENT_DIR / "symbol_names.json"

# 个股的资产类别用板块名，别用「美股」—— 组合里同时有 SPY 和 NVDA 时，
# 按资产类别分组的那张饼图要看得出后者是集中的行业敞口，不是宽基。
SECTORS = ("科技股", "通信股", "可选消费", "必需消费", "金融股", "医疗股",
           "工业股", "能源股", "公用事业", "原材料", "房地产")


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

    # ---- 行业板块 ETF（SPDR Select Sector）----
    # 这一族整体登记，不等 agent 一只一只报。它是拆开的 SPY：11 只加起来
    # 就是标普500，所以 agent 想表达"看好某个板块"时几乎必然从这里选 ——
    # 与其等它漏报一次、dashboard 上就空一格，不如一次写全。
    "XLK": SymbolInfo("科技板块", "美股", "Technology Select Sector SPDR，标普500里的科技股，权重高度集中在苹果和微软"),
    "XLV": SymbolInfo("医疗板块", "美股", "Health Care Select Sector SPDR，药企+器械+医疗服务，防御性板块，和大盘相关性偏低"),
    "XLF": SymbolInfo("金融板块", "美股", "Financial Select Sector SPDR，银行/保险/资管，对利率和信用周期敏感"),
    "XLE": SymbolInfo("能源板块", "美股", "Energy Select Sector SPDR，石油天然气为主，跟油价走，和其余板块常常反着动"),
    "XLI": SymbolInfo("工业板块", "美股", "Industrial Select Sector SPDR，航空/机械/运输/国防，典型的顺周期"),
    "XLY": SymbolInfo("可选消费", "美股", "Consumer Discretionary Select Sector SPDR，亚马逊和特斯拉占了很大权重"),
    "XLP": SymbolInfo("必需消费", "美股", "Consumer Staples Select Sector SPDR，食品日用品，衰退里最抗跌的板块之一"),
    "XLU": SymbolInfo("公用事业", "美股", "Utilities Select Sector SPDR，高股息低增长，走势更像债券而不是股票"),
    "XLB": SymbolInfo("原材料", "美股", "Materials Select Sector SPDR，化工/金属/建材，跟大宗商品和全球制造业周期"),
    "XLRE": SymbolInfo("房地产板块", "美股", "Real Estate Select Sector SPDR，标普500里的 REITs，比 VNQ 窄"),
    "XLC": SymbolInfo("通信服务", "美股", "Communication Services Select Sector SPDR，2018年新设，实际是 Meta+Alphabet 加一堆媒体"),

    # ---- 个股 ----
    # 目前只登记 agent 已经持有的。其余的由它自己在 commit 时登记，见文件头。
    "GE": SymbolInfo("GE航空航天", "工业股",
                     "GE Aerospace。2024 年三分拆后只剩航空发动机，利润主要来自售后维修 —— "
                     "已经不是那个什么都做的 GE 了，别照着旧印象给它归类"),
    "JPM": SymbolInfo("摩根大通", "金融股",
                      "美国最大的银行，投行/零售/资管全牌照，常被当作整个银行业的风向标"),
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


# ---------------- agent 登记的名字 ----------------

_learned: dict[str, SymbolInfo] = {}
_learned_mtime: float | None = None


def _learned_table() -> dict[str, SymbolInfo]:
    """按 mtime 懒加载。

    dashboard 是长驻进程，agent 每天都在写这个文件。只在 import 期读一次的话，
    今天新登记的名字要等你重启 streamlit 才看得见 —— 而你不会想到要重启，
    只会以为登记没生效。

    这个文件坏掉不能影响任何东西：读不出来就退回上一次的结果，最坏是没有名字。
    """
    global _learned, _learned_mtime
    try:
        mtime = LEARNED_FILE.stat().st_mtime
    except OSError:
        return {}          # 还没有 agent 登记过 —— 正常状态，不是错误
    if mtime == _learned_mtime:
        return _learned

    try:
        raw = json.loads(LEARNED_FILE.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("顶层不是一个对象")
    except (json.JSONDecodeError, OSError, ValueError) as e:
        log.warning("%s 读不出来（%s），继续用上一次的结果", LEARNED_FILE.name, e)
        return _learned

    table = {}
    for code, v in raw.items():
        if not isinstance(v, dict):
            continue
        i = _clean(v.get("name"), v.get("asset"), v.get("detail"))
        if i:
            table[str(code).strip().upper()] = i
    _learned, _learned_mtime = table, mtime
    return _learned


def _clean(name, asset, detail) -> SymbolInfo | None:
    """把一条外部来的登记洗干净。洗不出合法的名字就返回 None。

    换行必须去掉：侧边栏的对照表是 markdown，名字里带个换行就能把整块排版
    撑烂。长度也要卡 —— 中文简称是塞进下拉框的，模型很容易写成一句话。
    """
    def flat(x, limit):
        return " ".join(str(x or "").split())[:limit]

    n = flat(name, 20)
    if not n:
        return None
    return SymbolInfo(n, flat(asset, 12) or "个股", flat(detail, 200))


def register_many(entries: dict) -> tuple[list[str], list[str]]:
    """把 agent 提交的中文名并进 symbol_names.json。返回 (新增的, 跳过的)。

    两条不覆盖的规则：

      SYMBOLS 里已有的     手写表是权威，agent 改不了我核过的条目。
      已经登记过的         一只票的名字不该每天换一次说法，那会让你翻旧日志时
                          对不上是同一只票。要改就人工改。

    写文件失败**不抛异常**。这是显示层的东西，没有资格中断一次已经通过
    全部硬约束校验的调仓 —— 和文件开头那个 stdout 编码的坑是同一个道理。
    """
    added, skipped = [], []
    if not isinstance(entries, dict) or not entries:
        return added, skipped

    current = dict(_learned_table())
    for code, v in entries.items():
        sym = str(code).strip().upper()
        if not sym:
            continue
        if sym in SYMBOLS or sym in current:
            skipped.append(sym)
            continue
        i = _clean(v.get("name"), v.get("asset"), v.get("detail")) \
            if isinstance(v, dict) else _clean(v, None, None)
        if not i:
            skipped.append(sym)
            log.warning("%s 的登记没有可用的中文名，跳过", sym)
            continue
        current[sym] = i
        added.append(sym)

    if added:
        try:
            LEARNED_FILE.write_text(
                json.dumps({k: v._asdict() for k, v in sorted(current.items())},
                           indent=2, ensure_ascii=False),
                encoding="utf-8")
        except OSError as e:
            log.warning("写不进 %s（%s）—— 名字没登记上，不影响交易",
                        LEARNED_FILE.name, e)
            return [], sorted(set(added) | set(skipped))
    return added, skipped


def info(symbol: str) -> SymbolInfo | None:
    """查不到返回 None，由调用方决定怎么降级显示。"""
    s = symbol.upper()
    if s.startswith(SYNTH_PREFIX):
        return SYNTH.get(s[len(SYNTH_PREFIX):])
    # 手写表优先。agent 登记的只用来补它没覆盖到的代码。
    return SYMBOLS.get(s) or _learned_table().get(s)


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
