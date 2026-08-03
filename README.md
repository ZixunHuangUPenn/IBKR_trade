# IBKR 量化交易学习工程

一个从零开始的量化交易项目骨架，分三层：**连通 IBKR → 可视化 → 策略研发**。
面向"会写 Python、没做过量化"的人。

已验证环境：Python 3.11 / pandas 3.0 / ib_async 2.1 / Windows 11。

---

## 5 分钟上手

```powershell
# 1. 环境（已经建好了，跳过重建）
.\.venv\Scripts\Activate.ps1

# 2. 不连 IBKR，先跑通整条链路，顺便把合成数据写进缓存
python scripts\00_offline_demo.py --save-cache

# 3. 打开可视化平台，用合成数据玩一遍
streamlit run dashboard\app.py
```

这一步跑完，你已经能看到净值曲线、回撤、持仓变化、指标对比了 —— 还没碰过 IBKR。
**先理解回测，再连券商**，顺序反了会走弯路。

接下来连真账户：

```powershell
copy .env.example .env      # 按需改端口
python scripts\01_test_connection.py     # 确认连得上
python scripts\02_download_data.py       # 下载真实历史数据
python scripts\03_run_backtest.py        # 用真实数据回测
python scripts\04_paper_trade.py --strategy 横截面动量   # 生成订单（默认只预演）
```

---

## 连接 IBKR 前必做

1. 下载并启动 **IB Gateway**（比 TWS 轻量，适合跑程序）或 TWS，用 **Paper 账户**登录
2. `Configure → Settings → API → Settings`：
   - ☑ Enable ActiveX and Socket Clients
   - ☑ **Read-Only API** ← 学习阶段务必勾上，从券商侧就杜绝误下单
   - Socket port：Gateway 用 `4002`，TWS 用 `7497`（这两个是 Paper）
   - Trusted IPs 加 `127.0.0.1`
3. `copy .env.example .env`，确认 `IB_PORT` 和上面一致

> Paper 账户需要在 IBKR 官网 Account Management 里单独开通，账号通常以 `D` 开头（实盘以 `U` 开头）。
> `01_test_connection.py` 会把这个打印出来，每次都确认一眼。

---

## 项目结构

```
config.py              全局配置 + 实盘保护（三道锁）
notify.py              告警推送（webhook，可选）
ibkr/
  connection.py        连接管理、事件循环处理、错误码翻译、断线重试
  account.py           净值/持仓/成交/快照
  market_data.py       历史K线下载 + parquet 缓存 + 限速处理
  execution.py         目标权重 → 订单 → 执行（含 DRY_RUN / whatIf / 熔断）
  reconcile.py         对账：实际持仓 vs 目标权重
backtest/
  engine.py            向量化回测引擎（防未来函数、权重漂移、换手成本）
  metrics.py           夏普/回撤/卡玛/换手 等指标
strategies/
  base.py              策略基类：价格表 → 目标权重表
  sma_cross.py         双均线趋势
  momentum.py          横截面动量、逆波动率、固定权重
dashboard/app.py       Streamlit 可视化平台（账户/行情/回测实验室/数据管理）
scripts/00~04          按顺序的四课时脚本
scripts/05_daily_job   无人值守日常作业（signal / trade 两段式）
scripts/setup_task.ps1 把日常作业注册成 Windows 计划任务
rebalancer.py          你原有的再平衡脚本（保留）
```

**核心抽象只有一个**：任何策略都是 `价格表 → 目标权重表`。

回测和实盘调用的是**同一个** `generate_weights()`：回测拿整张表，实盘拿最后一行。
这样就不会出现"回测里是这么算的、实盘代码里又是另一套"这种最难查的 bug。

---

## 学习路线

### 阶段一：把管道打通（1~2 周）

目标不是赚钱，是**建立对基础设施的信任**。

- [ ] 跑通 `00` → `04` 四个脚本
- [ ] 读懂 `ibkr/connection.py`，理解为什么要包一层
- [ ] 故意制造几个错误看看会发生什么：端口写错、clientId 冲突、代码拼错、一次下载 100 个标的
- [ ] 在 Paper 账户手动下一笔单，然后用 `01_test_connection.py` 把它读出来

**要点**：`clientId` 每个程序必须不同，否则会互相踢掉。IB Gateway 每天会自动重启一次，重启后 API 断开——生产环境必须处理重连。

### 阶段二：可视化与数据（1~2 周）

- [ ] 在 dashboard 的回测实验室里，把每个参数都拖一遍，观察指标怎么变
- [ ] 做这三个对照实验（`00_offline_demo.py` 里已经演示了）：
  - `lag` 从 1 改成 0 → 看未来函数能凭空变出多少收益
  - 手续费/滑点从 0 调到真实值 → 看成本吃掉多少
  - 调仓缓冲区从 0 调到 0.1 → 看换手率和收益怎么权衡
- [ ] 给 dashboard 加一个你自己想看的图

**要点**：dashboard 不持有 broker 连接，只读磁盘快照。看盘的东西挂了不能影响交易的东西。

### 阶段三：策略研发（长期）

按这个顺序做，每一步都比上一步难：

1. **复现基准**——先确认你的回测算出的"买入持有 SPY"和真实的 SPY 走势对得上。对不上就是引擎有 bug，别往下走。
2. **理解已有策略**——把 `strategies/` 里四个策略读懂，尤其是横截面动量为什么要跳过最近一个月。
3. **写第一个自己的策略**——继承 `Strategy`，实现 `generate_weights()`。建议从"改造现有策略"开始，比如给双均线加个波动率过滤。
4. **样本外验证**——用 2015-2020 调参，2021-至今检验。如果样本外崩了，说明前面是在拟合噪音。
5. **Paper 实盘跑 3 个月**——重点不是收益，是量化**回测和实盘的偏差**：成交价差多少、整股取整损失多少、信号延迟造成多少差异。
6. **考虑实盘**——只有当第 5 步的偏差你能解释清楚时。

**新手最容易掉的坑**，按危害排序：

| 坑 | 后果 | 本项目里的应对 |
|---|---|---|
| 未来函数 | 回测年化 80%，实盘亏钱 | 引擎强制 `shift(lag)` |
| 幸存者偏差 | 只测了活到今天的股票 | 先用 ETF，不碰个股 |
| 忽略交易成本 | 高频策略回测赚、实盘亏 | 成本按换手计，滑点单列 |
| 过拟合参数 | 参数一动结果就崩 | 做参数敏感性检查 |
| 未复权价格 | 长期收益被系统性低估 | 默认 `ADJUSTED_LAST` |
| 多次测试偏差 | 试 100 组参数总有一组好看 | 样本外验证 |

---

## 三道实盘保护

这个项目默认连不上实盘，需要**同时**满足三个条件才行：

1. `.env` 里 `IB_PORT` 改成实盘端口（4001/7496）
2. `.env` 里 `IB_ALLOW_LIVE=true`
3. `.env` 里 `IB_READONLY=false`

另外 `ibkr/execution.py` 里有熔断：单笔超 `MAX_ORDER_VALUE`、或总换手超净值 50%，直接拒绝执行**整个**订单计划——因为单笔异常通常意味着上游算错了，这时候执行剩下的单更危险。

> ⚠️ 一个容易误解的点：`ib_async` 的 `readonly=True` **不会**阻止 `placeOrder`，
> 它只影响连接时拉取哪些数据。真正的强制只读在 **TWS/Gateway 的 "Read-Only API" 勾选框**。
> 本项目在 `execution.py` 里自己补了一道客户端拦截，但**别只依赖它**——
> 学习阶段请务必在 Gateway 里把那个框勾上，那才是券商侧的硬保护。

下单前的三级演练：`DRY_RUN`（只打印）→ `--what-if`（让 IBKR 校验保证金但不成交）→ `--execute`。

> ⚠️ `--what-if` 靠不住，别把它当成一道通过了的关卡。
> `ib_async` 只在 IBKR 返回的 `orderState` 带保证金数据时才兑现请求，而 Paper Gateway
> 经常什么都不返回 —— 实测这个账户上恒定拿不到回执。代码已经改成显式报
> `whatIf-无回执` 并告警，而不是假装通过。
> 真正拦得住错误的是 `_preflight` 的熔断、`DRY_RUN`，以及 Gateway 侧的 Read-Only API 勾选框。

另外 `execute_plan` 在发出第一笔单之前会把所有合约先认一遍，有任何一个 IBKR 认不出来
就整体拒绝。`qualifyContracts` 的返回语义很容易看错：它返回的列表**长度永远等于入参个数**，
认不出来的位置放 `None` —— 所以 `if not ib.qualifyContracts(c)` 是个永远不会触发的空判断，
必须查 `conId`。

---

## 无人值守：常开机器 + Gateway 自动重启 + 定时任务

手动跑脚本和挂成定时任务，中间隔着的不是"加个 cron"，是三件事。

### 为什么拆成 signal / trade 两段

回测引擎的约定是 `lag=1`：**t 日收盘算信号，t+1 日持有**（见 `backtest/engine.py`）。
而 `04_paper_trade.py` 是"算完立刻下单"。手动跑时你自己掌握时点所以无所谓，
挂成定时任务就必须把时序钉死 —— 否则实盘表现会比回测**好**，而且是靠一个
你没意识到的时序偏移换来的，这种偏差最难发现，也最不可能持续。

```
signal 阶段（美东 17:10，收盘后）  更新数据 → 算目标权重 → 写 data/signals/pending.json
trade  阶段（美东 09:45，次日开盘后）读信号 → 撤残单 → 下单 → 对账 → 归档
```

顺带一个好处：信号先落盘，你晚上可以先看一眼再让它第二天执行。

### 搭起来

```powershell
# 1. 先验证管道（跑完整流程但不写信号，因此绝不会产生交易；周末也能验）
python scripts\05_daily_job.py --stage signal --strategy 横截面动量 --dry-run

# 2. 注册两个计划任务。时点填**美东时间**，脚本自动换算成本机时区。
#    默认 trade 阶段只预演，不下单。
.\scripts\setup_task.ps1 -Strategy 横截面动量

# 3. 观察一两周，确认每天都按时跑、信号合理、预演出来的订单没有离谱的
#    然后才加 -Execute
.\scripts\setup_task.ps1 -Strategy 横截面动量 -Execute

# 拆掉
.\scripts\setup_task.ps1 -Remove
```

日志在 `data/logs/YYYY-MM-DD_{signal,trade}.log`。查任务状态：

```powershell
Get-ScheduledTaskInfo -TaskName IBKR-Signal | Select LastRunTime,LastTaskResult
```

`LastTaskResult` 为 0 才算正常。作业的退出码是有意义的：**0 = 正常（含"今天本来就不该做事"），1 = 出问题了**。

### 它替你挡住了什么

| 情况 | 处理 |
|---|---|
| Gateway 正在自动重启，端口是关的 | 连接重试 5 次 × 60 秒（`IB_CONNECT_RETRIES`）；任务计划器再重试 5 次 × 10 分钟 |
| 机器当时关着 / 睡着 | 任务的 `StartWhenAvailable`，醒来后补跑 |
| 周末触发（周五的信号还在 pending 里） | trade 阶段直接退出，**不消费信号**，留到周一 |
| 美股假日 | 向 IBKR 查 `liquidHours` 判断休市，保留信号退出；signal 阶段则因日线不是今天而不产生信号 |
| signal 作业挂了好几天 | trade 阶段检查信号年龄，超过 4 天拒绝执行并告警 |
| signal 和 trade 同一天跑了 | 拒绝执行（那等于 lag=0，破坏回测时序） |
| 昨天的限价单没成交还挂着 | 下单前先 `cancel_all`，否则会和今天的新单叠成超额仓位 |
| 下完单仓位没到位 | `ibkr/reconcile.py` 对账，偏差超容忍度就告警（**只告警，不自动补单**） |
| 上次还没跑完又到点了 | 任务的 `MultipleInstances IgnoreNew` |
| 今天不是策略的调仓日 | signal 阶段记录 `rebalance_day`，trade 阶段遵守（见下） |

### 实盘必须遵守策略自己的调仓日历

策略用 `rebalance_mask()` 声明什么时候可以调仓（横截面动量是月末）。回测严格遵守它，
**实盘不遵守的话，跑的就不是被回测过的那个策略**。实测差距：

```
横截面动量，月末调仓        年换手  359%
同一策略，日频 + 1% 带宽    年换手 2222%     ← 差 6.2 倍
```

收益上几乎看不出区别，成本上差一个数量级 —— 这种偏差最难被发现。
所以 signal 阶段把 `rebalance_day` 写进信号，trade 阶段照办。
想每天都调就加 `--ignore-calendar`，但那之后回测结果就不再代表实盘了。

对账刻意不自动补单：自动补单的循环一旦写错（比如价格取错导致反复下单），损失不封顶。
发现偏差先告警、让人看一眼，这个决定值得为它承受一点麻烦。

### 通知

`.env` 里设 `NOTIFY_WEBHOOK`（任何接受 POST JSON 的 URL，Bark / Slack / 企业微信都行）。
不设就只写日志。

> 无人值守最危险的失败模式不是"崩了"，是"静悄悄地什么也没做"。
> 任务计划器里那个 `LastTaskResult` 你不会每天去看。

### 让 Gateway 自己起来（IBC）

Gateway 是 GUI 程序，关掉了就没人再打开它 —— 这是"忘了开 Gateway 导致当天没跑"的根源。
[IbcAlpha/IBC](https://github.com/IbcAlpha/IBC) 负责自动填账号密码、自动点掉那些
会卡住登录的弹窗（"接受协议"、"版本过期"）。那些弹窗最阴险：进程活着、端口不监听，
你的作业只看到"连不上"。

本机已装在 `C:\IBC`，配置文件在 **`%USERPROFILE%\Documents\IBC\config.ini`** ——
不是 `C:\IBC\config.ini`。IBC 刻意把配置和程序目录分开，这样重新解压升级 IBC
不会覆盖掉带密码的配置。放错位置的表现是启动即 `ERRORLEVEL = 1006`。

关键配置：

```ini
TradingMode=paper                       # 上实盘要改成 live
OverrideTwsApiPort=4002                 # 实盘 Gateway 是 4001
ReadOnlyApi=no                          # 显式允许下单，不依赖 GUI 里的勾选状态
AutoRestartTime=03:00 AM
ExistingSessionDetectedAction=primary   # 别被其他登录挤掉
AcceptIncomingConnectionAction=accept   # 不弹 API 连接确认框
IbLoginId= / IbPassword=                # 自己填
```

自启与自愈都走 `scripts/ibc_watchdog.ps1`：

- 启动文件夹的 `IBC Gateway.lnk` —— 登录后立刻检查并拉起
- 计划任务 `IBC-Gateway` —— 每 5 分钟检查一次

**不能让计划任务直接跑 `StartGateway.bat`**：它内部用 `start` 派生独立窗口后立即返回，
任务几秒就结束，`MultipleInstances=IgnoreNew` 那道防护完全落空 ——
每次重复触发都找不到"正在运行的实例"可忽略，于是每次都再开一个 Gateway。
后果不只是多几个进程，那是每 5 分钟一次的登录尝试。

看门狗用 IBC 的 Java 进程（命令行含 `ibcalpha.ibc.IbcGateway`）判断是否在跑，
而不是用 4002 端口：登录中（尤其等 2FA 时）端口还没起来但进程已经在了，
用端口判断会在最不该重启的时候重启。

实测：杀掉 Gateway 进程后，看门狗 18 秒内重新拉起并完成自动登录。

> ⚠️ `config.ini` 里是**明文密码**。文件 ACL 已收窄到当前用户，但这台机器的安全
> 就等于你的 IBKR 安全。上实盘前给 Gateway 单独建一个 IBKR username。

### 关于机器和登录

- 计划任务设为**仅在用户登录时运行**。IB Gateway 本来就是 GUI 程序，这台机器
  无论如何都得保持登录状态，所以这不是额外限制。
- Gateway 的 auto-restart 时间**不要**落在上面两个作业时点附近。
- 本机时区若不随美国一起进出夏令时（比如机器在国内），每年三月和十一月
  要重跑一次 `setup_task.ps1` 让它重新换算。
- 同一个 IBKR username 不能两处登录。Gateway 挂着的时候你再用手机 App 登录会
  互相踢掉，自动交易就断了。去官网 `Settings → Users & Access Rights`
  加一个 username 专供 Gateway，日常自己用另一个。

---

## 推荐的开源项目和资源

**必用**

- [ib-api-reloaded/ib_async](https://github.com/ib-api-reloaded/ib_async) —— 本项目用的库。原作者 Ewald de Wit 2024 年初过世后 `ib_insync` 已归档，IBKR 官方文档现在也指向 `ib_async`。**网上大量 `ib_insync` 教程的代码在 `ib_async` 里基本能直接用**，import 换掉即可。
- [IBKR 官方 API 文档](https://www.interactivebrokers.com/campus/ibkr-api-page/getting-started/) —— 错误码含义、限速规则、合约定义，遇到问题先查这里。

**读代码学工程**（阶段二之后再看）

- [pst-group/pysystemtrade](https://github.com/robcarver17/pysystemtrade) —— Rob Carver（《Systematic Trading》作者）的期货实盘系统，2026 年 1 月迁到 pst-group 组织、由 Andy Geach 维护。**最值得读的是它的 `docs/production.md`**，讲清楚了一个真实的自动化交易系统需要处理多少边界情况：数据校验、订单状态机、故障恢复、每日对账。看完你会明白你现在这个骨架还差多远。
- [nautechsystems/nautilus_trader](https://github.com/nautechsystems/nautilus_trader) —— Rust 内核 + Python API，事件驱动架构，回测和实盘用同一套代码路径。想认真做低延迟或高频时看它。
- [QuantConnect/Lean](https://github.com/QuantConnect/Lean) —— 完整的回测+实盘引擎，有 IB 券商插件。适合想要"全套现成方案"的人，代价是要接受它的框架约定。

**工具库**

- [vectorbt](https://github.com/polakowo/vectorbt) —— 极快的向量化回测，适合大规模参数扫描（几万组参数几秒钟）。免费版够用。
- [backtesting.py](https://github.com/kernc/backtesting.py) —— 轻量、API 友好，适合单标的策略快速验证。
- [quantstats](https://github.com/ranaroussi/quantstats) —— 一行代码生成专业的绩效报告 PDF/HTML。可以直接接到本项目的 `BacktestResult.returns` 上。

**书**（按阅读顺序）

1. 《Systematic Trading》(Rob Carver) —— 讲清楚为什么大部分人的策略会失败，以及仓位管理比选股重要得多
2. 《Advances in Financial Machine Learning》(López de Prado) —— 讲金融数据为什么不能直接套用标准 ML 流程
3. 《Quantitative Trading》(Ernest Chan) —— 偏入门，例子多

**关于 IBKR 的另一套 API**：IBKR 还有 REST 风格的 Client Portal / Web API。对于**自动化策略**，TWS API（也就是本项目用的）仍是标准选择——功能全、延迟低、生态成熟。Web API 更适合做网页端的账户管理集成。刚起步不用纠结。

---

## 常见问题

**连不上**：Gateway 开着吗？登录了吗？API 勾选了吗？端口对吗？`clientId` 被占用了吗？
`01_test_connection.py` 报错时会把这个检查清单打出来。

**下载数据报错 162**：要么没有该品种的行情权限，要么触发了限速（10 分钟约 60 次请求）。
等 10 分钟再试，或者减少标的数量。项目里已经强制每次请求间隔 2 秒。

**价格取到 nan**：没买实时行情订阅。`.env` 里设 `IB_MARKET_DATA_TYPE=3`（延迟行情，免费）。
研究阶段延迟 15 分钟完全够用。

**pip 装依赖时 UnicodeDecodeError**：`requirements.txt` 必须保持纯 ASCII——pip 用系统 locale（中文 Windows 是 GBK）解码这个文件。

**中文输出乱码**：`$env:PYTHONIOENCODING="utf-8"` 或者 `chcp 65001`。

---

## 下一步做什么

最有价值的三件事，按顺序：

1. **跑 `00_offline_demo.py`，把那三个对照实验的输出逐行看懂**。未来函数、交易成本、调仓频率——这三件事决定了你未来 90% 的回测是不是自欺欺人。
2. **连上 Paper 账户，下载真实数据，看你的回测能不能复现"买入持有 SPY"的真实历史**。这是对基础设施的验收测试。
3. **写你的第一个策略**。别急着追求高收益，先追求"我能解释这个策略为什么有效"。解释不了的，就算回测好看也不要上。
