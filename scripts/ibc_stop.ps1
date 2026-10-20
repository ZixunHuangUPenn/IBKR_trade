<#
.SYNOPSIS
    优雅关闭由 IBC 启动的 IB Gateway，并确认进程已经退出。

.DESCRIPTION
    交易完成后调用 C:\IBC\Stop.bat，让 IBC 自己关闭 Gateway。不要直接 Stop-Process：
    优雅退出会给 Gateway 保存设置和日志的机会，也不会在仍有 API 请求时突然掐断。
#>

param(
    [string]$IbcPath = "C:\IBC",
    [int]$VerifySeconds = 60,
    [string]$WaitForTask = "",
    [int]$WaitMinutes = 120
)

$ErrorActionPreference = "Stop"
$marker = "ibcalpha.ibc.IbcGateway"

function Get-GatewayProcess {
    Get-CimInstance Win32_Process -Filter "Name='java.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine.Contains($marker) }
}

# 兜底定时任务会在 10:00 触发。若交易因为连接重试等原因仍在运行，宁可让
# Gateway 多显示一会儿，也绝不能在下单/对账中途断开它。
if ($WaitForTask) {
    $waitDeadline = (Get-Date).AddMinutes($WaitMinutes)
    while ((Get-Date) -lt $waitDeadline) {
        $task = Get-ScheduledTask -TaskName $WaitForTask -ErrorAction SilentlyContinue
        if (-not $task -or $task.State -ne 'Running') { break }
        Write-Output "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $WaitForTask 仍在运行，等待结束后再关闭 Gateway"
        Start-Sleep -Seconds 15
    }
    $task = Get-ScheduledTask -TaskName $WaitForTask -ErrorAction SilentlyContinue
    if ($task -and $task.State -eq 'Running') {
        Write-Output "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  等待 $WaitMinutes 分钟后 $WaitForTask 仍在运行，不强行关闭 Gateway"
        exit 1
    }
}

$running = Get-GatewayProcess
$stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
if (-not $running) {
    Write-Output "$stamp  Gateway 本来就没运行，无需关闭"
    exit 0
}

$stopBat = Join-Path $IbcPath "Stop.bat"
if (-not (Test-Path $stopBat)) { throw "找不到 $stopBat" }

$pids = ($running | ForEach-Object { $_.ProcessId }) -join ","
Write-Output "$stamp  正在通过 IBC 优雅关闭 Gateway (PID $pids)"
& $stopBat | Out-Null

$deadline = (Get-Date).AddSeconds($VerifySeconds)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 2
    if (-not (Get-GatewayProcess)) {
        $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
        Write-Output "$stamp  Gateway 已关闭"
        exit 0
    }
}

$stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Write-Output "$stamp  发出 STOP 后 $VerifySeconds 秒 Gateway 仍未退出"
exit 1
