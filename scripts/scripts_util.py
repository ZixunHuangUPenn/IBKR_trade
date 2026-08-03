"""脚本之间共用的小工具。"""

from __future__ import annotations


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
