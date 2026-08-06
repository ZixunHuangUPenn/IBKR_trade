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
agent/                 AI 自主选股（不可回测，走单独的约束体系）
  screen.py            候选资格硬门槛：流动性/价格/历史长度/杠杆产品黑名单
  datapack.py          事实计算：喂给 AI 的每个数字都由它算，AI 不许自己编
  policy.py            提案校验 —— 唯一真正拦得住 AI 的东西
prompts/agent_trader.md  给 AI 的投资授权书（工作流程 + 决策纪律）
dashboard/app.py       Streamlit 可视化平台（账户/行情/回测实验室/数据管理）
scripts/00~04          按顺序的四课时脚本
scripts/05_daily_job   无人值守日常作业（signal / trade 两段式）
scripts/06_agent_signal  AI 自主选股的 prepare / fetch / commit 三阶段
scripts/run_agent.ps1  AI 每日编排：备料 → claude 决策 → 校验落信号
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

`.env` 里设 `NOTIFY_WEBHOOK`，然后**跑一次自检确认它真的通**：

```powershell
python notify.py --test
```

发出去的 payload 同时带 `title`/`body` 和 `text`，所以 **Bark、Slack、Telegram
都不用适配层**；Discord（要 `content`）、企业微信/飞书（要嵌套 `msgtype`）、
ntfy（要 `topic`/`message`）则需要改 payload 形状。

> 无人值守最危险的失败模式不是"崩了"，是"静悄悄地什么也没做"。
> 任务计划器里那个 `LastTaskResult` 你不会每天去看。

两条 `info` 级通知（"今日信号已生成"、"调仓完成"）是**心跳**。
有效的监控不只是"出错时告警"，更是"该来的没来" —— 调仓日晚上没收到心跳，
这件事本身就是最重要的信号，而只做错误告警的系统给不了你这个。

`notify()` 返回的是**确认送达**，不是"发出去了"。HTTP 200 不等于送达：
Bark 的 key 写错回 400，而企业微信、Telegram 这类是拿 200 + body 里的错误码
表示失败的。只看状态码会漏掉后者 —— 一个你信任的坏通道，比压根没有通道更糟。

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
- 计划任务 `IBC-Gateway` —— **每天只跑两次**，各在 Signal / Trade 作业前一小时

不是每 N 分钟轮询一次，是**只在需要 Gateway 之前才检查**。理由很简单：
除了那两个时点，Gateway 在不在跑都没人关心；轮询买来的那点"更早发现"，
换的是一天几百次唤醒。留一小时提前量是因为拉起 + 自动登录实测 18 秒就够，
一小时足够容纳一次失败后你自己介入。

**不能让计划任务直接跑 `StartGateway.bat`**：它内部用 `start` 派生独立窗口后立即返回，
任务几秒就结束，`MultipleInstances=IgnoreNew` 那道防护完全落空 ——
每次触发都找不到"正在运行的实例"可忽略，于是每次都再开一个 Gateway。
后果不只是多几个进程，那是一次又一次的重复登录尝试。

看门狗用 IBC 的 Java 进程（命令行含 `ibcalpha.ibc.IbcGateway`）判断是否在跑，
而不是用 4002 端口：登录中（尤其等 2FA 时）端口还没起来但进程已经在了，
用端口判断会在最不该重启的时候重启。

实测：看门狗一旦触发，杀掉的 Gateway 进程 18 秒内就能重新拉起并完成自动登录。

代价说清楚：两次触发之间没有任何人在看。Gateway 上午崩了，要等到 13:10
那次才会被发现和拉起 —— 这是刻意的取舍，因为那期间没有作业要跑。真正的风险
是**触发那次没成功**（比如 4002 被别的东西占着，看门狗会拒绝启动并退出码 1），
这时候一小时内没有第二次尝试，作业时点就会撞上一个死的 Gateway。
所以 `LastTaskResult` 值得偶尔看一眼：

```powershell
Get-ScheduledTaskInfo -TaskName IBC-Gateway | Select LastRunTime,LastTaskResult
```

时点是从 `IBKR-Signal` / `IBKR-Trade` 的实际触发时间各减一小时算出来的。
**重跑 `setup_task.ps1` 改了作业时点（含每年两次的夏令时换算）之后，
这两个触发器不会自动跟着走**，得重新设一遍：

```powershell
$sig = ([datetime](Get-ScheduledTask -TaskName IBKR-Signal).Triggers[0].StartBoundary).AddHours(-1)
$trd = ([datetime](Get-ScheduledTask -TaskName IBKR-Trade ).Triggers[0].StartBoundary).AddHours(-1)
Set-ScheduledTask -TaskName IBC-Gateway -Trigger @(
    (New-ScheduledTaskTrigger -Daily -At $sig),
    (New-ScheduledTaskTrigger -Daily -At $trd))
```

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

## AI 自主选股（可选，风险自负）

让 AI 每天收盘后自己选股、自己定仓位，第二天开盘执行。

### 先说清楚代价

这个项目的核心不变量是"回测和实盘用同一个函数"。**AI 决策打破了它**：
每天的决定依赖当天的推理，无法重放、无法回测、没有任何证据表明它有正期望。
你放弃的是"我知道这个策略长什么样"，换来的是灵活性。

所以整套设计的重点全部在**约束**上，而不是决策上。**先在 Paper 上跑够三个月。**

### 它长这样

```
prepare   Python 算事实：账户 + 持仓行情 + 市场环境 + 最近几天的决策
   ↓                                          → data/agent/context.md
claude    AI 读材料 → 提名候选拿数据 → 写提案  → data/agent/proposal.json
   ↓
commit    逐条校验硬约束，通过才落信号        → data/signals/pending.json
   ↓
次日开盘   05_daily_job.py --stage trade       ← 这一步完全没改
```

**AI 只能写提案，写不了订单。** 它没有可下单的 IBKR 连接（`IBKR_AGENT_SANDBOX`
从连接层封死），唯一的产出是一个 JSON 文件。那个文件要变成订单，必须先过
`agent/policy.py`，再过 05 的熔断、开市检查、残单清理、对账。

AI 那一环坏掉（幻觉、算错、被自己说服、跑挂了）的最坏结果是**今天不交易**，
也就是保持昨天的仓位 —— 那是唯一一个我们确定有人认可过的状态。

### 搭起来

```powershell
# 1. 空跑一遍验证管道（不写信号，周末也能跑）
.\scripts\run_agent.ps1 -DryRun -Model sonnet

# 2. 注册计划任务。Signal 那一半换成 AI，Trade 那一半原封不动。
.\scripts\setup_task.ps1 -Agent

# 3. 观察，然后才 -Execute
.\scripts\setup_task.ps1 -Agent -Execute

# 急停：AI 链路立刻停摆，删掉文件即恢复
"先停一下" | Out-File data\agent\HALT -Encoding utf8
```

`claude` 必须能在无人值守时通过认证 —— 跑一次 `claude setup-token` 生成长期
token，别等到某天凌晨才发现它卡在登录界面。

### 硬约束（.env 里改，立刻生效）

写在 prompt 里的是建议，写在 `config.py` 里的才是法律。**违反任何一条 =
整份提案作废、当天不交易**，不做"截断到上限后继续"——截断出来的组合是
AI 从没考虑过的东西，风险收益结构已经变了，而那个变化没经过任何人的判断。

| 约束 | 默认（激进档） | 挡的是什么 |
|---|---|---|
| `AGENT_MAX_WEIGHT` | 33% | 单票押太重 |
| `AGENT_MAX_POSITIONS` | 6 | 分散不足 / 过度分散 |
| `AGENT_MAX_GROSS` | 100% | 加杠杆 |
| `AGENT_MAX_TURNOVER` | 50%/天 | 每天推倒重来。口径是 `Σ\|Δw\|÷2`，空仓建满整个组合刚好 50% |
| 只做多 | 权重 ≥ 0 | 做空 |
| `AGENT_MIN_DOLLAR_VOL` | $50M | **流动性陷阱**——冲击成本不体现在任何你会看的数字上 |
| `AGENT_MIN_PRICE` / `AGENT_MIN_HISTORY` | $10 / 500根 | 低价股、数据不全算不出 200 日线 |
| 杠杆/反向 ETF 黑名单 | 见 `agent/screen.py` | TQQQ 这类流动性极好、前面几道全拦不住的东西 |
| `AGENT_MAX_DRAWDOWN` | 20% | 没有回测的策略唯一能有的止损：亏到这里自动写 HALT 停摆 |

校验跑**两遍**：`commit` 时一遍（堵"AI 写了违规提案"），`trade` 时再一遍
（堵"有东西绕过 commit 直接写了 pending.json"——AI 是能敲命令的）。
校验要放在数据被**使用**的地方，不能只放在被产生的地方。

### 怎么指导它

授权书在 [`prompts/agent_trader.md`](prompts/agent_trader.md)，直接改那个文件就行。
里面最要紧的不是操作流程，是决策纪律，其中三条最值钱：

- **默认动作是"什么都不做"**。LLM 有强烈的"既然叫我决策我就得做点什么"的
  冲动，而每次调仓都是确定的成本换不确定的收益。逻辑没变就不该动。
- **每个买入都要预先写好退出条件**，而且要具体到能执行（"跌破 50 日线约 $215"
  而不是"基本面恶化"）。没有退出条件的仓位会变成一个你不断为它找新理由的仓位。
- **它能看见自己前几天的决策和理由**（`data/agent/journal.jsonl` → context 第四节）。
  没有这个，AI 每天都是第一次见到这个组合，于是每天重新想一遍"现在最该买什么"，
  结果是无意义的高换手。这是 LLM 做投资决策最典型的失败模式。

还有一条是硬性的：**AI 只允许引用 `context.md` 里出现过的数字**。所有指标由
`agent/datapack.py` 算好喂给它，它不许自己推导。LLM 算数会错，而且错得很自信,
你没法从输出里分辨哪个数是算的、哪个是编的。

默认**不联网**（`--tools` 白名单里根本没有 WebSearch/WebFetch）。它的训练数据
有截止日期，而市场早就把那些消息消化完了 —— 基于过时新闻做判断比不判断更糟。

### 每天去看什么

```powershell
Get-Content data\logs\*_agent_run.log -Tail 30      # 编排日志（含每天花了多少钱）
Get-Content data\agent\journal.jsonl                # 决策流水：买了什么、为什么、被拒过几次
Get-Content data\agent\context.md                   # 今天喂给它的全部材料
```

被拒绝的提案也会进 journal，下次 prepare 会把它喂回给 AI —— 让它看见自己错在哪，
否则同一个错误它会一直犯。

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
