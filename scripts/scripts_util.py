"""脚本之间共用的小工具。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def parse_params(items: list[str]) -> dict:
    """把命令行的 ['top_n=3', 'lookback=6'] 转成 {'top_n': 3, 'lookback': 6}。"""
    out: dict = {}
    for kv in items:
        if "=" not in kv:
            raise ValueError(f"参数格式应为 key=value，收到 {kv!r}")
        k, v = kv.split("=", 1)
        for cast in (int, float):
            try:
                out[k] = cast(v)
                break
            except ValueError:
                continue
        else:
            out[k] = v
    return out


# ---------------------------------------------------------------- 作业启动时刻

# FILETIME 是 1601-01-01 UTC 起算的 100 纳秒数。
_FILETIME_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)


def _process_start_utc() -> datetime | None:
    """本进程是什么时候被创建的。取不到就返回 None。

    它只是个兜底，没有资格因为自己失败而中断作业 —— 所以整段裹在 except 里。
    """
    try:
        import ctypes
        from ctypes import wintypes

        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.GetCurrentProcess.restype = wintypes.HANDLE
        k.GetCurrentProcess.argtypes = []
        k.GetProcessTimes.restype = wintypes.BOOL
        k.GetProcessTimes.argtypes = ([wintypes.HANDLE] +
                                      [ctypes.POINTER(wintypes.FILETIME)] * 4)

        ft = [wintypes.FILETIME() for _ in range(4)]
        if not k.GetProcessTimes(k.GetCurrentProcess(),
                                 *[ctypes.byref(f) for f in ft]):
            return None
        ticks = (ft[0].dwHighDateTime << 32) | ft[0].dwLowDateTime
        return _FILETIME_EPOCH + timedelta(microseconds=ticks / 10)
    except Exception:  # noqa: BLE001 - 非 Windows、API 变动、权限……都不该炸
        return None


def resolve_as_of(raw, tz) -> tuple[datetime, datetime, timedelta]:
    """把"作业是什么时候启动的"和"现在几点"分开。

    无人值守作业里这两个不是一回事。机器中途睡过去，进程会在几小时后才醒过来
    接着跑 —— 2026-08-14 就是这样：周五 18:40 ET 启动的作业，到周六 02:57 ET
    才走到第一行判断，于是被当成"周末不决策"跳过，退出码还是 0，日志一片绿，
    而周五那天的信号就这么没了。

    交易日历的问题（今天该不该做事）在**启动那一刻**就有答案了，要问 as_of；
    数据新不新是现实问题，只能问墙上的钟 now。混用哪一个都会错，而且错的方向
    相反：用 now 判日历会丢掉整个交易日，用 as_of 判新鲜度会拿几天前的数据下单。

    as_of 按优先级取：
      ① --as-of 传进来的时刻 —— run_agent.ps1 传的是 PowerShell 自己的启动
         时刻，它在 python 进程之前，最接近"任务真正开始"
      ② 本进程的创建时间 —— 计划任务直接调 python 时没人传得了参数，靠这个兜底
      ③ 此刻 —— 前两者都拿不到时退化成原来的行为

    返回 (as_of, now, delay)，三者都带时区。
    """
    now = datetime.now(tz)

    if raw and str(raw).strip():
        # 传错了就炸。悄悄退回 now 等于把我们正在修的这个 bug 原样藏回去，
        # 而且藏得更深 —— 那时连"哪一环传错了"都看不出来。
        try:
            parsed = datetime.fromisoformat(str(raw).strip())
        except ValueError as e:
            raise ValueError(f"--as-of 不是合法的 ISO 8601 时间：{raw!r}（{e}）") from e
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=tz)
        as_of = parsed.astimezone(tz)
    else:
        started = _process_start_utc()
        as_of = started.astimezone(tz) if started else now

    return as_of, now, now - as_of
