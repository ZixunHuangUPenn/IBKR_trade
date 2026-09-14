<#
.SYNOPSIS
    把 05_daily_job.py 注册成两个 Windows 计划任务。

.DESCRIPTION
    时点按**美东时间**填写，脚本自动换算成本机时区 —— 因为交易日历是美东的，
    而任务计划器只认本机时间。手工换算迟早会错一次，而且错的那天你不会知道。

    注册两个任务：
      <前缀>-Signal   收盘后：更新数据 + 算信号，写 data/signals/pending.json
      <前缀>-Trade    次日开盘后：读信号 + 下单 + 对账

    加 -Agent 的话，Signal 那一半换成 AI 自主选股（run_agent.ps1）——
    Trade 那一半完全不变，因为两者产出的是同一份 pending.json。
    这是刻意的：不管信号是公式算的还是 AI 想的，执行环节的安全网都一样。

    默认 Trade 任务**不带 --execute**，也就是只预演。确认跑顺了再加 -Execute 重跑一次本脚本。

.EXAMPLE
    # 先注册成预演模式，观察一两周
    .\scripts\setup_task.ps1 -Strategy 横截面动量

    # 确认无误后，让 Trade 阶段真正下单
    .\scripts\setup_task.ps1 -Strategy 横截面动量 -Execute

    # 固定权重（静态配置）。weights 里不能有空格，否则会被拆成多个参数。
    .\scripts\setup_task.ps1 -Strategy 固定权重再平衡 `
        -Params "weights=QQQ:0.5,SPY:0.3,IWM:0.2" -Execute

    # AI 自主选股，先预演
    .\scripts\setup_task.ps1 -Agent

    # AI 自主选股，真下单
    .\scripts\setup_task.ps1 -Agent -Execute

    # 拆掉
    .\scripts\setup_task.ps1 -Remove

.NOTES
    固定权重默认**季度**调仓（rebalance_months=3），所以注册完不会马上换仓 ——
    Trade 阶段会遵守策略的调仓日历，一直等到季末那天才动手。
    想立刻换到新比例，用 04_paper_trade.py 手动跑一次，它不受调仓日历约束。

    -Agent 模式没有调仓日历（AI 每天都可以调），换手由 AGENT_MAX_TURNOVER 约束。
#>

param(
    [string]$Strategy     = "横截面动量",
    [string]$Params       = "",            # 例: "top_n=3 lookback=6"
    [string]$SignalTimeET = "17:10",       # 美东收盘 16:00，留足日线结算时间
    [string]$TradeTimeET  = "09:45",       # 美东开盘 09:30，避开开盘前 15 分钟的乱价
    [switch]$Execute,                      # 不加 = Trade 阶段只预演
    [ValidateSet("LMT","MKT")]
    [string]$OrderType    = "LMT",         # 延迟行情下 LMT 常挂不上，首次验证可用 MKT
    [string]$TaskPrefix   = "IBKR",
    [switch]$Remove,
    # ---- AI 自主选股（-Agent）----
    [switch]$Agent,
    [string]$Model        = "opus",
    [double]$MaxBudgetUsd = 3.0
)

$ErrorActionPreference = "Stop"

$root       = Split-Path -Parent $PSScriptRoot
$python     = Join-Path $root ".venv\Scripts\python.exe"
$job        = Join-Path $root "scripts\05_daily_job.py"
$runAgent   = Join-Path $root "scripts\run_agent.ps1"
$runTrade   = Join-Path $root "scripts\run_trade.ps1"
$psExe      = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$signalName = "$TaskPrefix-Signal"
$tradeName  = "$TaskPrefix-Trade"

# ---------------------------------------------------------------- 拆除模式
if ($Remove) {
    foreach ($n in @($signalName, $tradeName)) {
        $existing = Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue
        if ($null -ne $existing) {
            Unregister-ScheduledTask -TaskName $n -Confirm:$false
            Write-Host "已删除任务 $n" -ForegroundColor Yellow
        } else {
            Write-Host "任务 $n 不存在，跳过"
        }
    }
    return
}

# ---------------------------------------------------------------- 前置检查
if (-not (Test-Path $python)) { throw "找不到虚拟环境的 python: $python" }
if (-not (Test-Path $job))    { throw "找不到作业脚本: $job" }
if (-not (Test-Path $runTrade)) { throw "找不到执行编排脚本: $runTrade" }
if ($Agent -and -not (Test-Path $runAgent)) { throw "找不到 AI 编排脚本: $runAgent" }

# ------------------------------------------------- 美东时间 -> 本机时间
$etTz    = [System.TimeZoneInfo]::FindSystemTimeZoneById("Eastern Standard Time")
$localTz = [System.TimeZoneInfo]::Local

function Convert-EtToLocal {
    param([string]$HHmm)
    $p = $HHmm.Split(":")
    if ($p.Count -ne 2) { throw "时间格式应为 HH:mm，收到 '$HHmm'" }
    # ConvertTimeToUtc 要求 Kind=Unspecified，否则会拿本机时区去解释它
    $naive = [datetime]::SpecifyKind(
        (Get-Date).Date.AddHours([int]$p[0]).AddMinutes([int]$p[1]), 'Unspecified')
    $utc = [System.TimeZoneInfo]::ConvertTimeToUtc($naive, $etTz)
    return [System.TimeZoneInfo]::ConvertTimeFromUtc($utc, $localTz)
}

$signalLocal = Convert-EtToLocal $SignalTimeET
$tradeLocal  = Convert-EtToLocal $TradeTimeET

# ---------------------------------------------------------------- 参数拼装
$paramArg = ""
if ($Params -ne "") { $paramArg = " --params $Params" }

if ($Agent) {
    # AI 模式：Signal 那一半交给 PowerShell 编排脚本（它内部再调 python 和 claude）
    $signalExe  = $psExe
    $signalArgs = "-NoProfile -ExecutionPolicy Bypass -File `"$runAgent`" " +
                  "-Model $Model -MaxBudgetUsd $MaxBudgetUsd"
    $signalDesc = "IBKR 日常作业：AI 自主选股，收盘后生成目标权重信号"
    $stratLabel = "AI 自主选股 (model=$Model)"
} else {
    $signalExe  = $python
    $signalArgs = "`"$job`" --stage signal --strategy `"$Strategy`"$paramArg"
    $signalDesc = "IBKR 日常作业：收盘后更新数据并生成目标权重信号"
    $stratLabel = "$Strategy $Params"
}

# Trade 也走 PowerShell 壳，不再直接调 python —— 壳里那层「别让机器睡了」
# 需要一个能盖住整段 python 执行的进程来持有。交易逻辑本身一行没改。
$tradeArgs = "-NoProfile -ExecutionPolicy Bypass -File `"$runTrade`" -OrderType $OrderType"
if ($Execute) { $tradeArgs += " -Execute" }

# ---------------------------------------------------------------- 注册
function Register-JobTask {
    param([string]$Name, [string]$Exe, [string]$Arguments, [datetime]$At,
          [string]$Description)

    $action = New-ScheduledTaskAction -Execute $Exe -Argument $Arguments -WorkingDirectory $root
    $trigger = New-ScheduledTaskTrigger -Daily -At $At

    # StartWhenAvailable  机器当时睡着/关着，醒来后补跑（无人值守的关键）
    # WakeToRun           到点自己把机器唤醒，不用等你开盖
    # Restart*            任务失败自动重试 —— Gateway 重启窗口、网络抖动都靠它
    # IgnoreNew           上一次还没跑完就不要再起一个，避免重复下单
    #
    # WakeToRun 单独打开是不够的：电源计划里的「允许唤醒定时器」在**电池模式下
    # 默认是关的**（RTCWAKE: AC=1, DC=0），而笔记本恰恰最可能在电池上过夜。
    # 下面会一并把 DC 那一档打开，否则这个开关是个哑弹。
    $settings = New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -WakeToRun `
        -RestartInterval (New-TimeSpan -Minutes 10) `
        -RestartCount 5 `
        -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
        -MultipleInstances IgnoreNew `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries

    # 刻意**不**提权。run_agent.ps1 作业期间要用 powercfg 改关盖动作，一开始
    # 以为得 Highest 级，实测不用 —— 改当前用户自己的电源方案本来就不需要管理员。
    # 这件事值得写下来：Signal 任务里跑的是一个拿着 Bash、--permission-mode
    # dontAsk 的 agent。给它一个提权进程，等于把沙箱封条之外的爆炸半径白白放大
    # 一圈，换来的却是一个它根本不需要的权限。
    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger `
        -Settings $settings -Description $Description -Force | Out-Null
}

function Enable-WakeTimersOnBattery {
    <#
        打开电池模式下的唤醒定时器。没有它，WakeToRun 在电池上不会生效 ——
        而 2026-08-24 那次作业迟到 32 分钟（要等人开盖才补跑）就是这么来的。
    #>
    $old = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & powercfg /setdcvalueindex SCHEME_CURRENT SUB_SLEEP RTCWAKE 1 | Out-Null
        if ($LASTEXITCODE -ne 0) { return $false }
        & powercfg /setactive SCHEME_CURRENT | Out-Null
        return ($LASTEXITCODE -eq 0)
    } catch { return $false }
    finally { $ErrorActionPreference = $old }
}

Register-JobTask -Name $signalName -Exe $signalExe -Arguments $signalArgs `
    -At $signalLocal -Description $signalDesc
Register-JobTask -Name $tradeName -Exe $psExe -Arguments $tradeArgs `
    -At $tradeLocal -Description "IBKR 日常作业：读取待执行信号，下单并对账"

$wakeOk = Enable-WakeTimersOnBattery

# ---------------------------------------------------------------- 汇总
$mode = "预演（不下单）"
if ($Execute) { $mode = "真实下单 / $OrderType" }

Write-Host ""
Write-Host "已注册两个计划任务：" -ForegroundColor Green
Write-Host ("  {0,-14} 美东 {1}  ->  本机 {2}" -f $signalName, $SignalTimeET, $signalLocal.ToString("HH:mm"))
Write-Host ("  {0,-14} 美东 {1}  ->  本机 {2}   [{3}]" -f $tradeName, $TradeTimeET, $tradeLocal.ToString("HH:mm"), $mode)
Write-Host ""
Write-Host ("  策略      {0}" -f $stratLabel)
Write-Host ("  本机时区  {0} (UTC{1:hh\:mm})" -f $localTz.Id, $localTz.BaseUtcOffset)
Write-Host ("  日志      {0}" -f (Join-Path $root "data\logs"))
Write-Host ""
Write-Host "注意：" -ForegroundColor Yellow
Write-Host "  - 任务只在你登录着的时候跑。IB Gateway 本来就是 GUI 程序，"
Write-Host "    这台机器无论如何都得保持登录状态，所以这不是额外限制。"
Write-Host "  - 本机时区若不随美国一起进出夏令时，每年三月和十一月要重跑一次本脚本。"
Write-Host "  - Gateway 的 auto-restart 时间不要落在上面两个时点附近。"
if ($wakeOk) {
    Write-Host "  - 已打开电池模式下的唤醒定时器，两个任务都会把机器叫醒来跑。"
} else {
    Write-Host "  - 电池模式下的唤醒定时器没打开成功，电池上机器不会自己醒："  -ForegroundColor Yellow
    Write-Host "    powercfg /setdcvalueindex SCHEME_CURRENT SUB_SLEEP RTCWAKE 1" -ForegroundColor Yellow
}
Write-Host "  - 两个任务跑的时候都会临时把「合盖」改成不休眠，跑完立刻还原（崩了的话"
Write-Host "    下次启动第一件事就是还原）。**只动交流电那一档** —— 电池下合盖照睡，"
Write-Host "    免得笔记本在包里空转到发烫。所以想关盖走人，记得插着电。"
if ($Agent) {
    Write-Host "  - claude 必须能在无人值守时通过认证。跑一次 'claude setup-token'"
    Write-Host "    生成长期 token，别等到某天凌晨才发现它在等你登录。"
    Write-Host "  - 急停：新建 data\agent\HALT 文件，AI 链路立刻停摆（删掉即恢复）。"
    Write-Host ("  - 硬约束在 config.py 的 AGENT_* 里，改完立刻生效，不用重注册任务。")
}
Write-Host ""
Write-Host "先手动验一遍（不会下单）：" -ForegroundColor Cyan
Write-Host "  Start-ScheduledTask -TaskName $signalName"
Write-Host "  Get-ScheduledTaskInfo -TaskName $signalName | Select LastRunTime,LastTaskResult"
