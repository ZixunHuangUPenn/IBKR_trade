<#
.SYNOPSIS
    次日开盘后执行信号的那一步，外面裹一层「别让机器睡了」。

.DESCRIPTION
    这层壳不碰任何交易逻辑 —— 下单、熔断、开市检查、残单清理、对账全部还在
    05_daily_job.py --stage trade 里，一行没动。它只做一件事：在 python 跑的
    这段时间里，不让笔记本进 Modern Standby。

    为什么这一步也需要：2026-08-24 的教训是 agent 那边丢了一天信号，看着像
    "反正只是少调一次仓"。但 trade 这一步睡过去更难看 —— 它中间会往 IBKR 发
    单。虽然平时只跑 51 秒，但 Gateway 也可能正从待机里爬（8/24 那次光重连
    就花了 2 分钟），窗口能拉长到好几分钟，正好够电池模式那 300 秒空闲超时踩进来。

    退出码原样透传 python，计划任务看的就是这个。

.EXAMPLE
    # 预演，不下单（默认）
    .\scripts\run_trade.ps1

    # 真下单
    .\scripts\run_trade.ps1 -OrderType MKT -Execute
#>

param(
    [ValidateSet("LMT","MKT")]
    [string]$OrderType = "LMT",
    [switch]$Execute
)

$ErrorActionPreference = "Stop"

$startedAt = Get-Date
$root   = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
$job    = Join-Path $root "scripts\05_daily_job.py"
$logDir = Join-Path $root "data\logs"

if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Force $logDir | Out-Null }
$logFile = Join-Path $logDir ("{0:yyyy-MM-dd}_trade_run.log" -f $startedAt)

function Write-Log {
    param([string]$Message, [string]$Level = "INFO")
    $line = "{0:yyyy-MM-dd HH:mm:ss} [{1}] run_trade: {2}" -f (Get-Date), $Level, $Message
    Write-Host $line
    Add-Content -Path $logFile -Value $line -Encoding utf8
}

function Send-Alert {
    param([string]$Title, [string]$Body)
    # 告警发不出去绝不能反过来把作业弄崩 —— 它是观测手段，不是业务逻辑。
    try { & $python (Join-Path $root "notify.py") --send $Title $Body --level error | Out-Null }
    catch { Write-Log "告警发送失败（不影响作业）：$_" "WARN" }
}

# 逻辑跟 run_agent.ps1 共用一份。关盖动作是全局状态，两份实现会互相把对方
# 存下来的原值覆盖掉，细节见模块头。
# 契约：点源之前必须已经定义好 Write-Log 和 Send-Alert。
. (Join-Path $PSScriptRoot "power_hold.ps1")

if (-not (Test-Path $python)) { throw "找不到虚拟环境的 python: $python" }
if (-not (Test-Path $job))    { throw "找不到作业脚本: $job" }

Write-Log ("=" * 60)
Write-Log "执行阶段启动（order-type=$OrderType$(if ($Execute) { '，真实下单' } else { '，预演' })）"

Enter-PowerHold

# 到脚本结尾都包在 try/finally 里，保证不管 python 返回什么都把关盖策略还回去。
try {

Set-Location $root

$jobArgs = @($job, "--stage", "trade", "--order-type", $OrderType)
if ($Execute) { $jobArgs += "--execute" }

& $python $jobArgs
$rc = $LASTEXITCODE

# 不在这里发告警：05_daily_job.py 对自己的失败已经喊过了（对账不通过、
# 信号过期、Gateway 连不上都有各自的通知）。这层再喊一遍只会变成重复噪声。
if ($rc -ne 0) { Write-Log "执行阶段退出码 $rc，详见当日 trade 日志。" "ERROR" }
else           { Write-Log "执行阶段结束。" }

exit $rc

} finally {
    # exit 也会走到这里。走不到的只剩硬杀（8/24 那种控制台被关），
    # 那种情况由存盘文件兜底：下次启动第一件事就是还原。
    Exit-PowerHold
}
