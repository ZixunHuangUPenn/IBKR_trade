<#
.SYNOPSIS
    把 05_daily_job.py 注册成两个 Windows 计划任务。

.DESCRIPTION
    时点按**美东时间**填写，脚本自动换算成本机时区 —— 因为交易日历是美东的，
    而任务计划器只认本机时间。手工换算迟早会错一次，而且错的那天你不会知道。

    注册两个任务：
      <前缀>-Signal   收盘后：更新数据 + 算信号，写 data/signals/pending.json
      <前缀>-Trade    次日开盘后：读信号 + 下单 + 对账

    默认 Trade 任务**不带 --execute**，也就是只预演。确认跑顺了再加 -Execute 重跑一次本脚本。

.EXAMPLE
    # 先注册成预演模式，观察一两周
    .\scripts\setup_task.ps1 -Strategy 横截面动量

    # 确认无误后，让 Trade 阶段真正下单
    .\scripts\setup_task.ps1 -Strategy 横截面动量 -Execute

    # 拆掉
    .\scripts\setup_task.ps1 -Remove
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
    [switch]$Remove
)

$ErrorActionPreference = "Stop"

$root       = Split-Path -Parent $PSScriptRoot
$python     = Join-Path $root ".venv\Scripts\python.exe"
$job        = Join-Path $root "scripts\05_daily_job.py"
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

$signalArgs = "`"$job`" --stage signal --strategy `"$Strategy`"$paramArg"
$tradeArgs  = "`"$job`" --stage trade --order-type $OrderType"
if ($Execute) { $tradeArgs += " --execute" }

# ---------------------------------------------------------------- 注册
function Register-JobTask {
    param([string]$Name, [string]$Arguments, [datetime]$At, [string]$Description)

    $action = New-ScheduledTaskAction -Execute $python -Argument $Arguments -WorkingDirectory $root
    $trigger = New-ScheduledTaskTrigger -Daily -At $At

    # StartWhenAvailable  机器当时睡着/关着，醒来后补跑（无人值守的关键）
    # Restart*            任务失败自动重试 —— Gateway 重启窗口、网络抖动都靠它
    # IgnoreNew           上一次还没跑完就不要再起一个，避免重复下单
    $settings = New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -RestartInterval (New-TimeSpan -Minutes 10) `
        -RestartCount 5 `
        -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
        -MultipleInstances IgnoreNew `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries

    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger `
        -Settings $settings -Description $Description -Force | Out-Null
}

Register-JobTask -Name $signalName -Arguments $signalArgs -At $signalLocal `
    -Description "IBKR 日常作业：收盘后更新数据并生成目标权重信号"
Register-JobTask -Name $tradeName -Arguments $tradeArgs -At $tradeLocal `
    -Description "IBKR 日常作业：读取待执行信号，下单并对账"

# ---------------------------------------------------------------- 汇总
$mode = "预演（不下单）"
if ($Execute) { $mode = "真实下单 / $OrderType" }

Write-Host ""
Write-Host "已注册两个计划任务：" -ForegroundColor Green
Write-Host ("  {0,-14} 美东 {1}  ->  本机 {2}" -f $signalName, $SignalTimeET, $signalLocal.ToString("HH:mm"))
Write-Host ("  {0,-14} 美东 {1}  ->  本机 {2}   [{3}]" -f $tradeName, $TradeTimeET, $tradeLocal.ToString("HH:mm"), $mode)
Write-Host ""
Write-Host ("  策略      {0} {1}" -f $Strategy, $Params)
Write-Host ("  本机时区  {0} (UTC{1:hh\:mm})" -f $localTz.Id, $localTz.BaseUtcOffset)
Write-Host ("  日志      {0}" -f (Join-Path $root "data\logs"))
Write-Host ""
Write-Host "注意：" -ForegroundColor Yellow
Write-Host "  - 任务只在你登录着的时候跑。IB Gateway 本来就是 GUI 程序，"
Write-Host "    这台机器无论如何都得保持登录状态，所以这不是额外限制。"
Write-Host "  - 本机时区若不随美国一起进出夏令时，每年三月和十一月要重跑一次本脚本。"
Write-Host "  - Gateway 的 auto-restart 时间不要落在上面两个时点附近。"
Write-Host ""
Write-Host "先手动验一遍（不会下单）：" -ForegroundColor Cyan
Write-Host "  Start-ScheduledTask -TaskName $signalName"
Write-Host "  Get-ScheduledTaskInfo -TaskName $signalName | Select LastRunTime,LastTaskResult"
