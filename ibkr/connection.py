"""
连接管理。

为什么要包一层，而不是直接 ib.connect()：
  1. 每次连接前强制过一遍实盘保护（config.assert_safe_to_connect）
  2. 保证断开 —— 忘记 disconnect 会占着 clientId，下次连不上
  3. 处理事件循环：ib_async 的同步 API 依赖 asyncio 事件循环，
     在子线程里（比如 Streamlit 的回调）默认没有循环，必须自己建
  4. 过滤 IBKR 那些"看起来像错误其实是正常心跳"的消息
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Callable, TypeVar

from ib_async import IB

import config

log = logging.getLogger("ibkr.connection")

# 这些 code 是连接状态通知，不是错误。不过滤掉的话日志会被刷屏。
_BENIGN_CODES = {2104, 2106, 2107, 2108, 2119, 2158, 2100, 2150}

T = TypeVar("T")


def _ensure_event_loop() -> None:
    """子线程里没有默认事件循环，ib_async 同步调用会直接崩。"""
    try:
        asyncio.get_event_loop_policy().get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())


def _attach_error_logger(ib: IB) -> None:
    def on_error(reqId, errorCode, errorString, contract):
        if errorCode in _BENIGN_CODES:
            log.debug("IB[%s] %s", errorCode, errorString)
        elif errorCode == 162:
            log.warning("历史数据被拒 (162): %s —— 通常是没有该品种的行情权限，"
                        "或请求过于频繁触发限速", errorString)
        elif errorCode in (354, 10089, 10090):
            log.warning("行情权限不足 (%s): %s —— 试试把 IB_MARKET_DATA_TYPE 设为 3（延迟行情）",
                        errorCode, errorString)
        elif errorCode == 200:
            log.error("合约无法识别 (200): %s (reqId=%s)", errorString, reqId)
        else:
            log.error("IB[%s] %s%s", errorCode, errorString,
                      f" [{contract.symbol}]" if contract else "")

    ib.errorEvent += on_error


class IBConnection:
    """
    用法：
        with IBConnection() as ib:
            print(ib.accountSummary())

    退出时一定断开，异常也一样。
    """

    def __init__(
        self,
        host: str = None,
        port: int = None,
        client_id: int = None,
        readonly: bool = None,
        timeout: float = 20.0,
    ):
        self.host = host or config.HOST
        self.port = port or config.PORT
        self.client_id = client_id if client_id is not None else config.CLIENT_ID
        self.readonly = config.READONLY if readonly is None else readonly
        self.timeout = timeout
        self.ib: IB | None = None

    def __enter__(self) -> IB:
        config.assert_safe_to_connect(self.port)
        _ensure_event_loop()

        ib = IB()
        _attach_error_logger(ib)
        try:
            ib.connect(
                self.host,
                self.port,
                clientId=self.client_id,
                timeout=self.timeout,
                readonly=self.readonly,
            )
        except (ConnectionRefusedError, OSError, asyncio.TimeoutError) as e:
            raise RuntimeError(
                f"连不上 {self.host}:{self.port} —— {e}\n"
                f"检查清单：\n"
                f"  1. TWS 或 IB Gateway 开着吗？登录进去了吗？\n"
                f"  2. Configure -> API -> Settings 里勾了 'Enable ActiveX and Socket Clients' 吗？\n"
                f"  3. Socket port 是不是 {self.port}？\n"
                f"  4. 'Trusted IPs' 里有 127.0.0.1 吗？\n"
                f"  5. clientId={self.client_id} 是否被别的程序占用了（换一个试试）？"
            ) from e

        # 没有行情订阅时，必须显式要延迟行情，否则 marketPrice() 返回 nan
        ib.reqMarketDataType(config.MARKET_DATA_TYPE)

        # 重要：ib_async 的 readonly 参数只影响连接时拉取哪些数据，
        # 它**不会**阻止 placeOrder。真正的只读强制是在 TWS/Gateway 那边
        # 勾 "Read-Only API"。这里把我们自己的意图记在 IB 对象上，
        # 供 execution 层做客户端侧的拦截。
        ib.readonly_mode = self.readonly

        mode = "只读" if self.readonly else "可下单"
        env = "实盘" if config.is_live_port(self.port) else "Paper"
        log.info("已连接 %s:%s [%s / %s / clientId=%s]",
                 self.host, self.port, env, mode, self.client_id)
        if config.is_live_port(self.port) and not self.readonly:
            log.warning(">>> 实盘 + 可下单模式。每一笔都是真钱。 <<<")

        self.ib = ib
        return ib

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.ib and self.ib.isConnected():
            self.ib.disconnect()
            log.info("已断开。")
        self.ib = None


def connect(**kwargs) -> IBConnection:
    """语法糖：with connect() as ib: ..."""
    return IBConnection(**kwargs)


def run_isolated(fn: Callable[[IB], T], **conn_kwargs) -> T:
    """
    在独立线程 + 独立事件循环里跑一次 IBKR 交互，然后彻底收摊。

    给 Streamlit 用的。Streamlit 每次交互都重跑整个脚本，
    长连接会和它的执行模型打架，不如每次开一条短连接。
    """
    box: dict = {}

    def target():
        try:
            with IBConnection(**conn_kwargs) as ib:
                box["value"] = fn(ib)
        except BaseException as e:  # noqa: BLE001 - 要把异常搬回主线程
            box["error"] = e

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join()

    if "error" in box:
        raise box["error"]
    return box["value"]
