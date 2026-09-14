<#
.SYNOPSIS
    作业期间不让这台机器睡过去。run_agent.ps1 和 run_trade.ps1 共用。

.DESCRIPTION
    2026-08-24：这台笔记本在 agent 跑到一半时自己睡过去了。电池模式空闲 300 秒
    -> Modern Standby -> 15 秒后网卡被 Adaptive Connected Standby 切断 -> claude
    那条 HTTPS 长流断在半路。进程同时被冻住，连超时都没跑成，直到 4 分半后被人
    敲键盘唤醒才反应过来，报 "Response stalled mid-stream"。$0.49 和一天的信号
    一起没了。

    这台机器 powercfg /a 只有 S0 Low Power Idle，没有 S3；能强制网卡在待机中保持
    连接的 CONNECTIVITYINSTANDBY 在这个平台上压根没暴露。也就是说**一旦真进了
    待机就没救** —— 只能不让它进去。

    睡眠有两个互相独立的入口，得分开堵：

      空闲超时   SetThreadExecutionState   进程级，进程一死自动失效
      合上盖子   只能改全局 LIDACTION       全局状态，必须由我们自己恢复

    关盖是用户主动下的指令，没有任何进程级 API 能否决它 —— 这就是第二行非得动
    全局设置的原因，也是它唯一让人不舒服的地方。所以按两条原则收窄暴露面：
      ① 只接管交流电那一档。电池下关盖照睡，免得笔记本在包里空转到发烫、耗干。
      ② 只在作业跑的那几分钟里接管，结束立刻还回去。

    两个都不需要管理员 —— 改当前用户自己的电源方案本来就不用提权。这一点值得
    写下来：Signal 任务里跑的是一个拿着 Bash、--permission-mode dontAsk 的 agent，
    为了这个功能去提权，等于白白放大沙箱之外的爆炸半径。

.NOTES
    用法（点源引入。调用方必须**先**定义 Write-Log 和 Send-Alert）：

        . "$PSScriptRoot\power_hold.ps1"
        Enter-PowerHold
        try { ...作业... } finally { Exit-PowerHold }

    Enter/Exit 之间不要 return，finally 是唯一保证还原的地方。
#>

# 调用方契约。缺了就当场炸，别等到作业跑一半才发现日志写不出去。
foreach ($fn in @("Write-Log", "Send-Alert")) {
    if (-not (Get-Command $fn -CommandType Function -ErrorAction SilentlyContinue)) {
        throw "power_hold.ps1 需要调用方先定义 $fn 函数。"
    }
}

# L 后缀不能省。PowerShell 5.1 的**十六进制**字面量不会自动加宽：裸写
# 0x80000000 会先被当成 [int] 溢出成 -2147483648，再转 [uint32] 直接抛异常。
# 叠上调用方的 ErrorActionPreference=Stop，效果是整个作业每天死在一个常量
# 定义上。（十进制字面量反而会自动加宽，所以 2147483648 是好的。）
$ES_CONTINUOUS      = [uint32]0x80000000L
$ES_SYSTEM_REQUIRED = [uint32]0x00000001L

$LID_SUB  = "4f971e89-eebd-4455-a8de-9e59040e7347"   # SUB_BUTTONS
$LID_SET  = "5ca83367-6e45-459f-a27b-476b1d01c936"   # LIDACTION（这台机器上是隐藏项）
$LID_KEEP = 0                                        # 0=不操作 1=睡眠 2=休眠 3=关机

# 原值存盘。作业被硬杀时 finally 是跑不到的 —— 8/24 那次就是（任务退出码
# 0xC000013A = STATUS_CONTROL_C_EXIT，控制台被关了）。所以恢复不能只挂在退出
# 路径上：下次启动先无条件读这个文件还原，再重新接管。
# 少了这一步，某次崩溃就能让这台笔记本从此关盖不睡，塞进包里是真会烧的。
#
# 两个作业**必须共用同一个文件**。各存各的会这样烂掉：Signal 接管后把值改成
# 0，Trade 启动时读到的"原值"就是 0，它一还原就把 0 写死了 —— 从此再没人知道
# 真正的原值是 1。共用一个文件时，后启动的那个会先把前一个的值还原掉，
# 读到的永远是真原值。
$LID_STASH = Join-Path (Split-Path -Parent $PSScriptRoot) "data\lid_restore.txt"

try {
    Add-Type -Namespace IbkrJob -Name Power -MemberDefinition @'
[DllImport("kernel32.dll", SetLastError = true)]
public static extern uint SetThreadExecutionState(uint esFlags);
'@
} catch { Write-Log "SetThreadExecutionState 绑定失败，空闲休眠挡不住了：$_" "WARN" }

function Get-ActiveScheme {
    (Get-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Control\Power\User\PowerSchemes" `
        -ErrorAction Stop).ActivePowerScheme
}

function Get-LidActionAc {
    # powercfg /q 不列隐藏项，所以直接读注册表。
    # 方案里没有显式值 = 在用出厂默认，那就把出厂默认取出来当原值 ——
    # 恢复的时候必须写回一个确定的数，不能靠猜。
    $scheme = Get-ActiveScheme
    $cur = (Get-ItemProperty `
        "HKLM:\SYSTEM\CurrentControlSet\Control\Power\User\PowerSchemes\$scheme\$LID_SUB\$LID_SET" `
        -ErrorAction SilentlyContinue).ACSettingIndex
    if ($null -ne $cur) { return [int]$cur }

    $def = (Get-ItemProperty `
        "HKLM:\SYSTEM\CurrentControlSet\Control\Power\PowerSettings\$LID_SUB\$LID_SET\DefaultPowerSchemeValues\$scheme" `
        -ErrorAction SilentlyContinue).ACSettingIndex
    if ($null -ne $def) { return [int]$def }

    return 1   # 最后兜底：睡眠。拿不准的时候宁可多睡，不可少睡。
}

function Set-LidActionAc {
    param([int]$Value)
    # powercfg 往 stderr 写东西时，5.1 会把它包成 NativeCommandError；叠上
    # ErrorActionPreference=Stop 就成了终止错误。电源策略没资格中断交易作业。
    $old = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & powercfg /setacvalueindex SCHEME_CURRENT $LID_SUB $LID_SET $Value | Out-Null
        if ($LASTEXITCODE -ne 0) { return $false }
        & powercfg /setactive SCHEME_CURRENT | Out-Null
        return ($LASTEXITCODE -eq 0)
    } catch { return $false }
    finally { $ErrorActionPreference = $old }
}

function Restore-LidAction {
    <#  把关盖动作还回去。没有存盘文件就是没接管过，直接返回。  #>
    if (-not (Test-Path $LID_STASH)) { return }

    $saved = ""
    try { $saved = (Get-Content $LID_STASH -Raw -ErrorAction Stop).Trim() } catch { }
    if ($saved -notmatch '^\d+$') {
        Write-Log "关盖动作存盘文件内容不合法（'$saved'），丢弃。" "WARN"
        Remove-Item $LID_STASH -Force -ErrorAction SilentlyContinue
        return
    }

    if (Set-LidActionAc ([int]$saved)) {
        Remove-Item $LID_STASH -Force -ErrorAction SilentlyContinue
        Write-Log "关盖动作已还原为 $saved。"
    } else {
        # 存盘文件**不删** —— 留着让下次启动接着试。这是唯一能兜住
        # "改成功了但没改回去"的东西，宁可多喊一次也不能丢。
        $msg = ("关盖动作没能还原（powercfg 写失败）。这台机器现在插着电时合盖" +
                "不会休眠。手动改回来：`n" +
                "  powercfg /setacvalueindex SCHEME_CURRENT $LID_SUB $LID_SET $saved`n" +
                "  powercfg /setactive SCHEME_CURRENT")
        Write-Log $msg "ERROR"
        Send-Alert "笔记本关盖策略没还原" $msg
    }
}

function Enter-PowerHold {
    # ① 空闲休眠。进程级，进程一死自动失效 —— 8/24 挂掉的就是这一关。
    try {
        $rc = [IbkrJob.Power]::SetThreadExecutionState($ES_CONTINUOUS -bor $ES_SYSTEM_REQUIRED)
        if ($rc -eq 0) { Write-Log "SetThreadExecutionState 返回 0，空闲休眠没挡住。" "WARN" }
        else           { Write-Log "已阻止空闲休眠（作业结束自动解除）。" }
    } catch { Write-Log "阻止空闲休眠失败（继续跑）：$_" "WARN" }

    # ② 关盖休眠。上一次没还回去的先还掉，再重新接管 ——
    #    顺序反了的话读到的"原值"会是上次接管后的 0，真原值就永久丢了。
    Restore-LidAction

    $orig = 1
    try { $orig = Get-LidActionAc } catch { Write-Log "读关盖动作失败：$_" "WARN"; return }
    if ($orig -eq $LID_KEEP) { Write-Log "关盖动作本来就是「不操作」，不接管。"; return }

    # 先落盘再改。反过来的话，改完那一瞬间进程挂掉就没人知道要还什么了。
    try { Set-Content -Path $LID_STASH -Value "$orig" -Encoding utf8 -ErrorAction Stop }
    catch { Write-Log "关盖动作存盘失败，放弃接管（不敢改一个还不回去的设置）：$_" "WARN"; return }

    if (Set-LidActionAc $LID_KEEP) {
        Write-Log "已接管关盖动作：交流电下合盖不休眠（原值 $orig，结束后还原）。"
    } else {
        Remove-Item $LID_STASH -Force -ErrorAction SilentlyContinue
        Write-Log ("接管关盖动作失败（powercfg 没写进去）。空闲休眠仍然挡住了，" +
                   "但作业期间合上盖子会中断 —— 想关盖走人就先手动跑：" +
                   "powercfg /setacvalueindex SCHEME_CURRENT $LID_SUB $LID_SET 0") "WARN"
    }
}

function Exit-PowerHold {
    Restore-LidAction
    # 空闲休眠的请求随进程消失，但显式解除一次，别留到进程退出前的那几秒。
    try { [IbkrJob.Power]::SetThreadExecutionState($ES_CONTINUOUS) | Out-Null } catch { }
}
