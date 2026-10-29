<#
.SYNOPSIS
    IBC 看门狗：Gateway 不在跑就把它拉起来，在跑就什么都不做。

.DESCRIPTION
    为什么需要这一层，而不是直接让计划任务去跑 StartGateway.bat：

    StartGateway.bat 内部用 `start` 派生了一个独立的控制台窗口，然后**立即返回**。
    所以计划任务几秒钟就结束了，任务状态回到 Ready —— 这意味着
    `MultipleInstances=IgnoreNew` 那道防护完全不起作用：每次触发时都没有
    "正在运行的实例"可供忽略，于是每次触发都会再开一个 Gateway。

    后果不只是多几个进程：那是一次又一次的重复登录尝试，IBKR 会把它当成异常行为。

    调度策略见 README：不轮询，每天只在 Signal / Trade 作业前一小时各跑一次。

    所以判断"是否已在运行"必须自己做。判据用 IBC 的 Java 进程而不是 4002 端口：
    登录中（尤其等 2FA 时）端口还没起来，但进程已经在了 —— 用端口判断会在
    最不该重启的时候重启。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\ibc_watchdog.ps1
#>

param(
    [string]$IbcPath = "C:\IBC",
    [int]$VerifySeconds = 90,   # 拉起之后等多久确认进程真的在了
    [int]$StaleProcessMinutes = 10,
    [switch]$WhatIfOnly         # 只报告状态，不启动
)

$ErrorActionPreference = "Stop"

$marker = "ibcalpha.ibc.IbcGateway"

function Get-GatewayProcess {
    Get-CimInstance Win32_Process -Filter "Name='java.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine.Contains($marker) }
}

$running = @(Get-GatewayProcess)
$stamp   = Get-Date -Format "yyyy-MM-dd HH:mm:ss"

$portBusy = $null -ne (Get-NetTCPConnection -LocalPort 4002 -State Listen -ErrorAction SilentlyContinue)
if ($running.Count -gt 0) {
    $pids = ($running | ForEach-Object { $_.ProcessId }) -join ","
    if ($portBusy) {
        Write-Output "$stamp  Gateway 在跑且 4002 正常监听 (PID $pids)，无需处理"
        exit 0
    }

    # 手工关掉 Gateway 窗口后，IBC Java 进程偶尔会残留。旧版只看进程就报成功，
    # 结果 Agent 看到的是 ConnectionRefused。给正常登录留十分钟；超过后仍无端口，
    # 明确判为假活，清掉残留进程再重新拉起。
    $oldest = $running | Sort-Object CreationDate | Select-Object -First 1
    $ageMinutes = ((Get-Date) - $oldest.CreationDate).TotalMinutes
    if ($ageMinutes -lt $StaleProcessMinutes) {
        Write-Output "$stamp  Gateway 进程刚启动 (PID $pids，${ageMinutes} 分钟)，4002 尚未监听；等待登录完成"
        exit 0
    }
    if ($WhatIfOnly) {
        Write-Output "$stamp  Gateway 进程假活 (PID $pids)：运行 ${ageMinutes} 分钟但 4002 未监听"
        exit 0
    }
    Write-Output "$stamp  Gateway 进程假活 (PID $pids)：运行 ${ageMinutes} 分钟但 4002 未监听，正在重启"
    $running | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 3
}

# 端口还开着但 IBC 进程没了 = 有别的东西占着 4002，这时候拉起 Gateway 只会撞车
elseif ($portBusy) {
    Write-Output "$stamp  IBC 进程不在，但 4002 被占用 —— 不启动，需人工确认"
    exit 1
}

if ($portBusy -and $running.Count -eq 0) {
    # 上面的 elseif 已处理；保留显式保护，避免以后改分支时撞端口。
    Write-Output "$stamp  4002 被占用 —— 不启动"
    exit 1
}

if ($WhatIfOnly) {
    Write-Output "$stamp  Gateway 未运行（WhatIfOnly，不启动）"
    exit 0
}

$bat = Join-Path $IbcPath "StartGateway.bat"
if (-not (Test-Path $bat)) { throw "找不到 $bat" }

Write-Output "$stamp  Gateway 未运行，正在拉起 $bat"
Start-Process -FilePath $bat -WorkingDirectory $IbcPath -WindowStyle Minimized

# 拉起之后必须确认它真的起来了，不能发完命令就退出 0。
# 启动会失败：配置文件放错位置（ERRORLEVEL 1006）、IBC 版本和 Gateway 对不上、
# 磁盘满。这些情况下 bat 一样会"正常返回"，看门狗要是跟着报成功，
# 计划任务的"失败后重试"就永远不会触发 —— 等于没有重试。
# 实测拉起 + 自动登录 18 秒，给到 90 秒是留够 2FA 和冷启动的余量。
$deadline = (Get-Date).AddSeconds($VerifySeconds)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 3
    $up = Get-GatewayProcess
    if ($up) {
        $pids = ($up | ForEach-Object { $_.ProcessId }) -join ","
        $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
        Write-Output "$stamp  已拉起 (PID $pids)"
        exit 0
    }
}

# 退出码非 0 是给任务计划器看的信号：它会按 RestartInterval 每 5 分钟再来一次。
$stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Write-Output "$stamp  拉起后 $VerifySeconds 秒内没看到 Gateway 进程 —— 判定失败，等待重试"
exit 1
