"""
第 1 课：确认能连上 IBKR，并看懂账户里的每个数。

    python scripts/01_test_connection.py

跑之前：
  1. 启动 IB Gateway 或 TWS，用 **Paper** 账户登录
  2. Configure -> API -> Settings:
       [x] Enable ActiveX and Socket Clients
       [x] Read-Only API        <- 学习阶段先勾上，从源头杜绝误下单
       [ ] Allow connections from localhost only（勾不勾都行）
       Socket port: Gateway=4002 / TWS=7497
       Trusted IPs 里加 127.0.0.1
  3. 复制 .env.example 成 .env

注意：IB Gateway 每天有一次自动重启（默认凌晨），重启后 API 会断。
      生产环境要处理重连，学习阶段知道有这回事就行。
"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
from ibkr import account as acct
from ibkr.connection import IBConnection

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def main() -> int:
    print("=" * 72)
    print(f"目标: {config.HOST}:{config.PORT}  "
          f"({'实盘' if config.is_live_port() else 'Paper'})")
    print(f"clientId={config.CLIENT_ID}  只读={config.READONLY}  "
          f"行情类型={config.MARKET_DATA_TYPE}")
    print("=" * 72)

    with IBConnection() as ib:
        print(f"\nTWS/Gateway 版本: {ib.client.serverVersion()}")
        accounts = ib.managedAccounts()
        print(f"可用账户: {accounts}")
        if config.ACCOUNT and config.ACCOUNT not in accounts:
            print(f"  ⚠ .env 里的 IB_ACCOUNT={config.ACCOUNT} 不在上面的列表里")

        # Paper 账号通常以 D 开头，实盘以 U 开头 —— 再确认一次你连的是哪个
        for a in accounts:
            kind = "Paper" if a.startswith("D") else "实盘" if a.startswith("U") else "?"
            print(f"  {a}  ({kind})")

        print("\n--- 账户摘要 ---")
        s = acct.account_summary(ib)
        for k, v in s.items():
            print(f"  {k:22} {v:>15,.2f}")

        print("\n--- 持仓 ---")
        pos = acct.positions_df(ib)
        if pos.empty:
            print("  （空仓）")
        else:
            print(pos.to_string(index=False))

        print("\n--- 未成交订单 ---")
        oo = acct.open_orders_df(ib)
        print("  （无）" if oo.empty else oo.to_string(index=False))

        print("\n--- 今日成交 ---")
        fl = acct.fills_df(ib)
        print("  （无）" if fl.empty else fl.to_string(index=False))

        acct.save_snapshot(ib)
        print(f"\n快照已写入 {config.SNAPSHOT_DIR}")

    print("\n连接测试通过。")
    print("下一步: python scripts/02_download_data.py")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as e:
        print(f"\n[失败] {e}", file=sys.stderr)
        raise SystemExit(1)
