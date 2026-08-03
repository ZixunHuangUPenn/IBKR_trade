"""
告警通道。

无人值守作业最危险的失败模式不是"崩了"，是"静悄悄地什么也没做"。
任务计划器里那个 Last Run Result 你不会每天去看，所以出事必须主动推给你。

刻意做得很薄：一个 POST，超时就算了。
通知发不出去绝不能反过来把交易作业弄崩 —— 它是观测手段，不是业务逻辑。

自检：
    python notify.py --test
"""

from __future__ import annotations

import logging

import config

log = logging.getLogger("notify")

TIMEOUT = 10


def _delivered(resp) -> bool:
    """
    判断"真的送达了"。HTTP 200 不等于送达 —— 这是这个模块最容易骗人的地方。

    Bark 的 key 写错会回 HTTP 400；而企业微信、Telegram 这类接口
    是拿 HTTP 200 + body 里的错误码表示失败的。只看状态码会漏掉后者，
    结果就是你以为告警通道通着，其实它已经死了几个月 ——
    一个你信任的坏通道，比压根没有通道更糟。
    """
    if not 200 <= resp.status_code < 300:
        log.warning("通知未送达: HTTP %s %s", resp.status_code, resp.text[:200])
        return False

    try:
        data = resp.json()
    except ValueError:
        return True                      # 纯文本响应（Slack 回 "ok"），认为成功
    if not isinstance(data, dict):
        return True

    if data.get("ok") is False:          # Telegram
        log.warning("通知未送达: %s", str(data)[:200])
        return False
    for key in ("code", "errcode"):      # Bark / 企业微信 / 飞书
        v = data.get(key)
        if isinstance(v, int) and v not in (0, 200):
            log.warning("通知未送达: %s", str(data)[:200])
            return False
    return True


def notify(title: str, body: str = "", level: str = "info") -> bool:
    """
    发一条通知。返回是否**确认送达**（不是"是否发出去了"）。

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
        # title/body 给 Bark 这类；text 给 Slack、Telegram 这类。
        # 同时带上，常见服务就都不用适配层了。
        resp = requests.post(
            config.NOTIFY_WEBHOOK,
            json={"title": title, "body": body, "text": text, "level": level},
            timeout=TIMEOUT,
        )
    except Exception as e:  # noqa: BLE001 - 通知失败绝不能影响主流程
        log.warning("通知发送失败（不影响交易）: %s", e)
        return False

    return _delivered(resp)


def _mask(url: str) -> str:
    return url[:22] + "…" + url[-4:] if len(url) > 30 else url


def main() -> int:
    """自检：确认告警通道真的通。配完 NOTIFY_WEBHOOK 就该跑一次。"""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if not config.NOTIFY_WEBHOOK:
        print("NOTIFY_WEBHOOK 没配。去 .env 里填一个接受 POST JSON 的 URL。")
        return 1

    print(f"目标: {_mask(config.NOTIFY_WEBHOOK)}")
    ok = notify(
        "IBKR 告警通道自检",
        "收到这条说明通道是通的。\n"
        "作业出问题时你会在这里看到：对账不通过、信号过期、Gateway 连不上。",
    )
    print("确认送达。" if ok else "未送达 —— 看上面的 warning。")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
