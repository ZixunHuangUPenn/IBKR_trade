<#
.SYNOPSIS
    AI 自主选股的每日编排：备料 -> 让 agent 决策 -> 校验落信号。

.DESCRIPTION
    这个脚本是计划任务真正调用的东西。它把三段串起来：

        ① 06_agent_signal.py --stage prepare   Python 算事实，写 data/agent/context.md
        ② claude -p                            agent 读材料、拉候选、写提案
        ③ 06_agent_signal.py --stage commit    逐条校验，通过才写 pending.json

    次日开盘后由 05_daily_job.py --stage trade 执行 —— 那一步完全没有改动，
    它的熔断、开市检查、残单清理、对账全部照旧。

    第 ② 步全程带着两道封条：
      IBKR_AGENT_SANDBOX=1   这一格里派生的任何 Python 进程都建不成可下单的连接
      --tools 白名单          agent 拿不到 WebSearch / WebFetch，它只能看本地数据

    退出码：0=正常（含"今天本来就不该做事"） 1=出问题了。计划任务看这个。

.EXAMPLE
    # 先空跑一遍，看管道通不通（不写信号，周末也能跑）
    .\scripts\run_agent.ps1 -DryRun

    # 正常跑一次
    .\scripts\run_agent.ps1

    # 换个模型、放宽预算
    .\scripts\run_agent.ps1 -Model sonnet -MaxBudgetUsd 5

.NOTES
    claude 需要能在无人值守时通过认证。用 `claude setup-token` 生成长期 token，
    或确认订阅登录态是持久的 —— 别等到某天凌晨才发现它在等你登录。
#>

param(
    [string]$Model         = "opus",
    [double]$MaxBudgetUsd  = 3.0,
    [int]$TimeoutMinutes   = 25,
    [string]$ClaudeBin     = "",       # 留空 = 自动找
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

$root    = Split-Path -Parent $PSScriptRoot
$python  = Join-Path $root ".venv\Scripts\python.exe"
$job     = Join-Path $root "scripts\06_agent_signal.py"
$mandate = Join-Path $root "prompts\agent_trader.md"
$logDir  = Join-Path $root "data\logs"
$agentDir= Join-Path $root "data\agent"

if (-not (Test-Path $logDir))   { New-Item -ItemType Directory -Force $logDir   | Out-Null }
if (-not (Test-Path $agentDir)) { New-Item -ItemType Directory -Force $agentDir | Out-Null }

$logFile = Join-Path $logDir ("{0:yyyy-MM-dd}_agent_run.log" -f (Get-Date))

function Write-Log {
    param([string]$Message, [string]$Level = "INFO")
    $line = "{0:yyyy-MM-dd HH:mm:ss} [{1}] run_agent: {2}" -f (Get-Date), $Level, $Message
    Write-Host $line
    Add-Content -Path $logFile -Value $line -Encoding utf8
}

function Send-Alert {
    param([string]$Title, [string]$Body)
    # 告警发不出去绝不能反过来把作业弄崩 —— 它是观测手段，不是业务逻辑。
    try { & $python (Join-Path $root "notify.py") --send $Title $Body --level error | Out-Null }
    catch { Write-Log "告警发送失败（不影响作业）：$_" "WARN" }
}

# ---------------------------------------------------------------- 找 claude
function Resolve-ClaudeBin {
    if ($ClaudeBin -ne "") {
        if (Test-Path $ClaudeBin) { return $ClaudeBin }
        throw "指定的 -ClaudeBin 不存在：$ClaudeBin"
    }
    if ($env:CLAUDE_BIN -and (Test-Path $env:CLAUDE_BIN)) { return $env:CLAUDE_BIN }

    $onPath = Get-Command claude -ErrorAction SilentlyContinue
    if ($null -ne $onPath) { return $onPath.Source }

    $standalone = Join-Path $env:USERPROFILE ".local\bin\claude.exe"
    if (Test-Path $standalone) { return $standalone }

    # 最后才退到 VSCode 扩展自带的那个。它的路径里带版本号，扩展一升级就变 ——
    # 能用，但不该作为长期方案，所以这里要吭声。
    $extRoot = Join-Path $env:USERPROFILE ".vscode\extensions"
    if (Test-Path $extRoot) {
        $bundled = Get-ChildItem $extRoot -Directory -Filter "anthropic.claude-code-*" |
                   Sort-Object Name -Descending |
                   ForEach-Object { Join-Path $_.FullName "resources\native-binary\claude.exe" } |
                   Where-Object { Test-Path $_ } |
                   Select-Object -First 1
        if ($bundled) {
            Write-Log ("用的是 VSCode 扩展自带的 claude：$bundled`n" +
                       "  这个路径带版本号，扩展升级后会失效。建议装独立版：`n" +
                       "  irm https://claude.ai/install.ps1 | iex") "WARN"
            return $bundled
        }
    }
    throw ("找不到 claude 可执行文件。装一个：irm https://claude.ai/install.ps1 | iex`n" +
           "或者用 -ClaudeBin 指定路径，或设环境变量 CLAUDE_BIN。")
}

# ---------------------------------------------------------------- 前置检查
if (-not (Test-Path $python))  { throw "找不到虚拟环境的 python: $python" }
if (-not (Test-Path $job))     { throw "找不到作业脚本: $job" }
if (-not (Test-Path $mandate)) { throw "找不到 agent 的工作说明: $mandate" }

$claude = Resolve-ClaudeBin

Write-Log ("=" * 60)
Write-Log "AI 自主选股作业启动$(if ($DryRun) { ' [dry-run]' })"
Write-Log "python : $python"
Write-Log "claude : $claude  (model=$Model, 预算上限 `$$MaxBudgetUsd)"

Set-Location $root

# ------------------------------------------------------- ① 备料
$prepArgs = @($job, "--stage", "prepare")
if ($DryRun) { $prepArgs += "--dry-run" }

& $python $prepArgs
$rc = $LASTEXITCODE

if ($rc -eq 10) {
    # "今天本来就不该做事"：周末、假日、人工急停、回撤熔断。
    # prepare 已经写好日志也发好通知了，这里安静退出，别让计划任务报红。
    Write-Log "prepare 判定今天不该决策，正常结束。"
    exit 0
}
if ($rc -ne 0) {
    Write-Log "prepare 失败（退出码 $rc）。不进入决策环节。" "ERROR"
    exit 1
}

# ------------------------------------------------------- ② agent 决策
#
# 封条：这一格里派生出来的任何 Python 进程都建不成可下单的连接。
# 强制点在 ibkr/connection.py，不接受命令行参数覆盖 ——
# 它防的不是手滑，是一个能自己敲命令的 agent 决定去跑执行环节。
$env:IBKR_AGENT_SANDBOX = "1"

$stdoutFile = Join-Path $agentDir "claude_stdout.json"
$stderrFile = Join-Path $agentDir "claude_stderr.txt"
$promptFile = Join-Path $agentDir "prompt.txt"

# prompt 从 stdin 喂，不走命令行参数。
#
# 这一条是踩出来的：把多行 prompt 放进 Start-Process -ArgumentList，
# 它在第一个空白处就被截断了 —— agent 只收到"你的完整工作说明在"这几个字，
# 然后开始反问我要完整内容。而且失败得很安静：claude 退出码 0、
# is_error=False，只有"没产出提案"这一个症状。
#
# 顺带一个好处：授权书全文直接进 prompt，不再依赖 agent 自己去 Read 那个文件。
$kickoff = @"


---

现在开始。第一步：读 data/agent/context.md。

即使你的结论是"今天什么都不改"，也必须把提案写进 data/agent/proposal.json
（把当前权重原样写一遍）。不写文件 = 视为作业失败，今天不会有任何交易。
"@

# 必须是不带 BOM 的 UTF-8。带 BOM 的话那三个字节会变成 prompt 的头几个字符。
[System.IO.File]::WriteAllText(
    $promptFile,
    ((Get-Content $mandate -Raw -Encoding utf8) + $kickoff),
    (New-Object System.Text.UTF8Encoding($false)))

$claudeArgs = @(
    "-p",
    "--model", $Model,
    "--effort", "high",
    # 只给这几个内建工具。WebSearch / WebFetch 直接不在集合里 ——
    # "不许联网"写在 prompt 里 agent 可以不听，从工具集里拿掉它就没得选。
    "--tools", "Read,Write,Edit,Bash,Glob,Grep",
    "--allowed-tools", "Read", "Glob", "Grep", "Write", "Edit", "Bash",
    # 真正拦得住的是 commit 阶段的校验，这几条 deny 只是第一道门：
    # 不许直接写待执行信号，不许改自己的约束。
    "--disallowed-tools",
        "Write(data/signals/**)", "Edit(data/signals/**)",
        "Write(config.py)", "Edit(config.py)",
        "Write(agent/policy.py)", "Edit(agent/policy.py)",
        "Write(agent/screen.py)", "Edit(agent/screen.py)",
        "WebSearch", "WebFetch",
    "--permission-mode", "dontAsk",
    "--max-budget-usd", "$MaxBudgetUsd",
    "--output-format", "json",
    "--no-session-persistence",
    "--add-dir", $root
)

Write-Log "调用 claude 决策（超时 $TimeoutMinutes 分钟）…"
$started = Get-Date

$proc = Start-Process -FilePath $claude -ArgumentList $claudeArgs `
    -WorkingDirectory $root -NoNewWindow -PassThru `
    -RedirectStandardInput $promptFile `
    -RedirectStandardOutput $stdoutFile -RedirectStandardError $stderrFile

# 碰一下 .Handle 才能拿到 ExitCode。
# 不碰的话 $proc.ExitCode 是 $null，而 `$null -ne 0` 为真 ——
# 于是每次都会报"claude 退出码非 0"，一条永远在响的假警报。
$null = $proc.Handle

$timedOut = -not $proc.WaitForExit($TimeoutMinutes * 60 * 1000)
if ($timedOut) {
    # 挂死的 agent 比失败的 agent 更麻烦：它会一直占着计划任务的执行槽，
    # 明天那次就因为 MultipleInstances=IgnoreNew 而根本不启动。所以必须杀。
    #
    # 但杀完仍然继续走校验，不在这里 exit。
    # 实测过一次：agent 已经把一份完全合格的提案写好了，只是收尾那几十秒
    # 拖过了超时线 —— 直接放弃等于把做完的活扔掉。而"继续"是安全的：
    # commit 本来就要把提案从头验一遍（含 signal_date 是不是今天），
    # 半截的、旧的、违规的提案都过不去。
    try { $proc.Kill() } catch { }
    $msg = "claude 超过 $TimeoutMinutes 分钟没结束，已强制终止。仍会尝试校验已写出的提案。"
    Write-Log $msg "WARN"
    Send-Alert "AI 决策超时" $msg
}

$claudeRc = if ($timedOut) { "已超时终止" } else { $proc.ExitCode }
$elapsed = (Get-Date) - $started
Remove-Item Env:\IBKR_AGENT_SANDBOX -ErrorAction SilentlyContinue

Write-Log ("claude 结束，退出码 $claudeRc，耗时 {0:mm\:ss}" -f $elapsed)

# 把成本和轮数记进日志。跑上一个月你才知道这东西一年要花多少钱。
if (Test-Path $stdoutFile) {
    try {
        $res = Get-Content $stdoutFile -Raw -Encoding utf8 | ConvertFrom-Json
        Write-Log ("本次成本 `$$([math]::Round($res.total_cost_usd, 4))，{0} 轮，is_error={1}" -f $res.num_turns, $res.is_error)
        # 正则要用单引号。写成 "`r?`n" 的话反引号先被 PowerShell 解释成真的
        # CR / LF 字符，交给 -replace 的就不是 \r?\n 这个模式了。
        if ($res.result) { Write-Log ("agent 结语：" + ($res.result -replace '\r?\n', ' | ')) }
    } catch {
        Write-Log "解析 claude 输出失败（不影响后续，提案文件才是准的）：$_" "WARN"
    }
}
if ((Test-Path $stderrFile) -and (Get-Item $stderrFile).Length -gt 0) {
    Write-Log ("claude stderr: " + (Get-Content $stderrFile -Raw)) "WARN"
}

if ($claudeRc -ne 0) {
    # 不在这里退出 —— 说不定它在崩之前已经把提案写好了。
    # 交给 commit 去判断：有没有提案、提案合不合规，那才是唯一算数的事。
    Write-Log "claude 退出码非 0，但仍继续走校验（提案可能已写好）。" "WARN"
}

# ------------------------------------------------------- ③ 校验并落信号
$commitArgs = @($job, "--stage", "commit")
if ($DryRun) { $commitArgs += "--dry-run" }

& $python $commitArgs
$rc = $LASTEXITCODE

if ($rc -ne 0) {
    # commit 自己已经发过详细告警了，这里只留一行结论。
    Write-Log "commit 未通过（退出码 $rc）。今天不产生信号，明天维持现有仓位。" "ERROR"
    exit 1
}

Write-Log "完成。信号已就绪，等次日 05_daily_job.py --stage trade 执行。"
exit 0
