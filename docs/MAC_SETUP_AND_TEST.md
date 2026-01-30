## Mac 转移与测试步骤（Codex 专用）

目标：在 macOS（arm64）上完整跑通 Ostium + Lighter 套利脚本（BTC 测试）。

### 1) 从 Git 拉取代码
```bash
git clone <你的仓库地址>
cd perp-dex-tools
```

### 2) 创建 Python 虚拟环境并安装依赖
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install ostium-python-sdk
```

### 3) 准备 .env
把 Windows 上的 `.env` 拷贝到项目根目录。

必要字段（示例）：
```
RPC_URL=...
PRIVATE_KEY=...
API_KEY_PRIVATE_KEY=...
LIGHTER_BASE_URL=https://mainnet.zklighter.elliot.ai
LIGHTER_ACCOUNT_INDEX=0
LIGHTER_API_KEY_INDEX=0
OSTIUM_LEVERAGE=5
OSTIUM_PRICE_OFFSET_BPS=5
```

### 4) 先跑监控（确认行情正常）
```bash
python scripts/monitor_ostium_lighter_top10.py --interval 60 --min-net-bps -5 --notional-usd 10000 --once
```

### 5) BTC 走完整流程测试（忽略利润）
注意：`--min-net-bps -10000` 只是为了走通流程，并不代表真实策略。
```bash
python scripts/arbitrage_ostium_lighter.py --symbol BTC --notional-usd 50 --min-net-bps -10000 --execute
```

### 6) 安全限制
脚本会自动把名义金额限制在 20~200 U 范围内，避免超额下单。

