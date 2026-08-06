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
    [switch]$WhatIfOnly      # 只报告状态，不启动
)

$ErrorActionPreference = "Stop"

$marker = "ibcalpha.ibc.IbcGateway"
$running = Get-CimInstance Win32_Process -Filter "Name='java.exe'" -ErrorAction SilentlyContinue |
           Where-Object { $_.CommandLine -and $_.CommandLine.Contains($marker) }

$stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"

if ($running) {
    $pids = ($running | ForEach-Object { $_.ProcessId }) -join ","
    Write-Output "$stamp  Gateway 在跑 (PID $pids)，无需处理"
    exit 0
}

# 端口还开着但进程没了 = 有别的东西占着 4002，这时候拉起 Gateway 只会撞车
$portBusy = $null -ne (Get-NetTCPConnection -LocalPort 4002 -State Listen -ErrorAction SilentlyContinue)
if ($portBusy) {
    Write-Output "$stamp  IBC 进程不在，但 4002 被占用 —— 不启动，需人工确认"
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
exit 0
