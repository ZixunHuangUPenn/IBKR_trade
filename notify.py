"""
告警通道。

无人值守作业最危险的失败模式不是"崩了"，是"静悄悄地什么也没做"。
任务计划器里那个 Last Run Result 你不会每天去看，所以出事必须主动推给你。

刻意做得很薄：一个 POST，超时就算了。
通知发不出去绝不能反过来把交易作业弄崩 —— 它是观测手段，不是业务逻辑。
"""

from __future__ import annotations

import logging

import config

log = logging.getLogger("notify")


def notify(title: str, body: str = "", level: str = "info") -> bool:
    """
    发一条通知。返回是否真的发出去了。

    level: info / warn / error —— 只是打进 payload，具体怎么区分交给接收端。
    NOTIFY_WEBHOOK 没配就只写日志，返回 False。
    """
    text = f"{title}\n{body}".strip()
    (log.error if level == "error" else log.warning if level == "warn" else log.info)(
        "[通知] %s", text.replace("\n", " | ")
    )

    if not config.NOTIFY_WEBHOOK:
        return False

    try:
        import requests
        requests.post(
            config.NOTIFY_WEBHOOK,
            json={"title": title, "body": body, "text": text, "level": level},
            timeout=10,
        )
        return True
    except Exception as e:  # noqa: BLE001 - 通知失败绝不能影响主流程
        log.warning("通知发送失败（不影响交易）: %s", e)
        return False
