# perp-dex-tools 代码库总结（AI 二次开发用）

本文档面向 **AI 辅助二次开发**：提供任务→文件映射、接口约定、扩展步骤与约束，便于在不破坏现有逻辑的前提下修改或新增功能。实现细节以仓库内源码为准。

---

## 使用本文档的方式（AI 优先）

- **接到任务时**：先看 [任务 → 文件/文档 映射](#任务--文件文档-映射)，确定要改动的文件和参考文档。
- **改接口或加交易所**：看 [exchanges/base.py 接口约定](#42-exchangesbasepy) 与 [docs/ADDING_EXCHANGES.md](#21-相关文档说明)。
- **改策略或主循环**：看 [trading_bot.py](#41-trading_botpy) 与 [数据流](#5-数据流与调用关系-runbot)。
- **查某功能在哪个文件**：看 [文件与函数速查](#8-文件与函数速查)。

---

## 任务 → 文件/文档 映射

| 任务 | 主要修改文件 | 参考 |
|------|----------------|------|
| 新增 runbot 可用交易所 | `exchanges/<新交易所>.py`，`exchanges/factory.py` | `docs/ADDING_EXCHANGES.md` |
| 新增对冲模式交易所 | `hedge/hedge_mode_<名>.py`，`hedge_mode.py`（`get_hedge_bot_class`、`validate_exchange`） | 现有 `hedge/hedge_mode_bp.py` 等 |
| 修改策略逻辑（下单/止盈/等待） | `trading_bot.py`（`TradingBot` 主循环、`_place_and_monitor_open_order`、`_handle_order_result`） | — |
| 新增/修改 runbot CLI 参数 | `runbot.py`（`parse_arguments`），`trading_bot.py`（`TradingConfig`） | — |
| 修改某交易所 API/WS 行为 | `exchanges/<交易所>.py` | `exchanges/base.py` 接口 |
| 新增通知渠道 | `trading_bot.py`（`send_notification`），可选 `helpers/` 新模块 | `helpers/telegram_bot.py`，`helpers/lark_bot.py` |
| 修改日志格式或输出 | `helpers/logger.py`（`TradingLogger`） | — |

---

## 1. 项目概述

- **仓库**：perp-dex-tools  
- **用途**：多交易所永续合约自动交易/刷量机器人。支持：限价开仓 → 止盈平仓；网格步长；止价/暂停价；Boost 模式（maker 开 + taker 平）；对冲模式（主交易所 + Lighter）。  
- **策略要点**：市价附近下限价单开仓，成交后按止盈比例挂平仓单；`max-orders` / `wait-time` / `grid-step` 控制风险与频率；**无止损**，需自行评估风险。  
- **运行环境**：Python 3.8+（Paradex 建议 3.9–3.12，grvt 需 3.10+）。依赖见根目录 `requirements.txt`、`para_requirements.txt`、`apex_requirements.txt`。

---

## 2. 目录结构

```
perp-dex-tools/
├── runbot.py              # 主入口：刷量/止盈策略
├── hedge_mode.py          # 对冲模式入口：按 --exchange 分发到 hedge/*
├── trading_bot.py         # 核心交易循环与订单监控
├── requirements.txt      # 主依赖
├── para_requirements.txt # Paradex 专用
├── apex_requirements.txt # Apex 专用
├── env_example.txt        # 环境变量示例
├── docs/
│   ├── ADDING_EXCHANGES.md        # 新增交易所完整步骤（含示例代码）
│   ├── telegram-bot-setup.md      # Telegram 通知配置
│   └── CODEBASE_SUMMARY_AI_DEV.md # 本文件
├── exchanges/             # 交易所客户端，均继承 BaseExchangeClient
│   ├── __init__.py
│   ├── base.py            # 抽象基类、OrderResult/OrderInfo、query_retry
│   ├── factory.py         # 按名称创建交易所实例（懒加载）
│   ├── edgex.py, backpack.py, paradex.py, aster.py, lighter.py,
│   ├── grvt.py, extended.py, apex.py, nado.py, standx.py
│   ├── lighter_custom_websocket.py
│   └── bp_client.py       # Backpack 账户封装
├── hedge/                 # 对冲：主交易所 + Lighter
│   ├── hedge_mode_bp.py, hedge_mode_ext.py, hedge_mode_apex.py,
│   ├── hedge_mode_grvt.py, hedge_mode_grvt_v2.py,
│   ├── hedge_mode_edgex.py, hedge_mode_nado.py, hedge_mode_standx.py
├── helpers/
│   ├── __init__.py
│   ├── logger.py          # TradingLogger
│   ├── telegram_bot.py    # Telegram（同步）
│   └── lark_bot.py        # 飞书 Webhook（异步）
└── tests/
    └── test_query_retry.py
```

### 2.1 相关文档说明

- **`docs/ADDING_EXCHANGES.md`**：**新增交易所**的完整指南。包含：实现 `BaseExchangeClient` 子类（含各方法示例）、在 `exchanges/factory.py` 注册、环境变量、OrderResult/OrderInfo 结构、最佳实践、现有交易所实现要点。**新增或对接新交易所时优先阅读此文档。**
- **`docs/telegram-bot-setup.md`**（及 `telegram-bot-setup-en.md`）：Telegram 机器人配置，用于交易/异常通知。

---

## 3. 入口与运行方式

### 3.1 刷量/止盈机器人（runbot.py）

- **流程**：CLI 解析 → 加载 `--env-file`（默认 `.env`）→ 构造 `TradingConfig` → `ExchangeFactory.create_exchange(exchange, config)` → `TradingBot(config).run()`。
- **关键参数**：`--exchange`、`--ticker`、`--quantity`、`--take-profit`、`--direction`、`--max-orders`、`--wait-time`、`--grid-step`、`--stop-price`、`--pause-price`、`--boost`、`--env-file`。
- **约束**：`--boost` 仅允许在 `aster`、`backpack` 使用；否则 runbot 会报错退出。

### 3.2 对冲模式（hedge_mode.py）

- **流程**：CLI 解析 → `validate_exchange(exchange)` → `get_hedge_bot_class(exchange, v2)` 动态导入 `hedge/hedge_mode_*.py` 中的 `HedgeBot` → 实例化并执行。
- **支持交易所**：backpack, extended, apex, grvt, edgex, nado, standx。grvt 可用 `--v2` 使用 `hedge_mode_grvt_v2`。
- **常用参数**：`--exchange`、`--ticker`、`--size`、`--iter`、`--fill-timeout`、`--sleep`、`--max-position`、`--env-file`。

---

## 4. 核心模块与类

### 4.1 trading_bot.py

| 名称 | 类型 | 说明 |
|------|------|------|
| `TradingConfig` | dataclass | 策略与合约参数：ticker, contract_id, quantity, take_profit, tick_size, direction, max_orders, wait_time, exchange, grid_step, stop_price, pause_price, boost_mode；只读属性 `close_order_side`（buy→sell，sell→buy）。 |
| `OrderMonitor` | dataclass | 订单监控状态：order_id, filled, filled_price, filled_qty；`reset()`。 |
| `TradingBot` | class | 主交易逻辑：连接交易所、主循环中更新 active_close_orders、价格条件、等待时间、grid-step、下单与监控、平仓；Boost 时开仓后调用 `place_market_order` 平仓。 |

**TradingBot 主要方法（扩展/修改策略时重点）**：

| 方法 | 作用 |
|------|------|
| `run()` | 获取 contract_id/tick_size → connect → 主循环（订单更新、周期日志、价格条件、等待、grid-step、下单）。 |
| `_place_and_monitor_open_order()` | 下限价开仓单；未即时成交则等 WebSocket/轮询，超时或价格不利则撤单，有成交则挂平仓（限价或 Boost 市价）。 |
| `_handle_order_result()` | 根据成交/撤单结果挂平仓单；未成交时轮询订单状态、撤单后按 filled 挂平仓。 |
| `_calculate_wait_time()` | 按当前平仓单数量与 max_orders 比例计算冷却时间（返回 0 表示可下单）。 |
| `_log_status_periodically()` | 汇总 active_close_orders、持仓；仓位与平仓量不一致时发通知并请求 shutdown。 |
| `_meet_grid_step_condition()` | 判断新平仓价与现有最近平仓单是否满足 grid_step 距离。 |
| `_check_price_condition()` | 返回 `(stop_trading, pause_trading)`。 |
| `send_notification(message)` | 若有 LARK_TOKEN / TELEGRAM_* 则发飞书/Telegram。 |
| `graceful_shutdown(reason)` | 置 shutdown 标志并 disconnect 交易所。 |
| `_setup_websocket_handlers()` | 向 `exchange_client` 注册订单更新回调；回调内更新 `current_order_status`、`order_filled_event`、`order_canceled_event` 等。 |

**TradingBot 依赖的交易所接口**：除 base 抽象方法外，必须实现 `get_contract_attributes`、`fetch_bbo_prices`、`get_order_price`；若支持 Boost，需实现 `place_market_order`。

### 4.2 exchanges/base.py

**装饰器**：

- `query_retry(default_return=..., exception_type=..., max_attempts=5, min_wait=1, max_wait=10, reraise=False)`：指数退避重试；失败时打印并返回 default_return（或 reraise）。

**数据结构**：

- `OrderResult`：success, order_id?, side?, size?, price?, status?, error_message?, filled_size?
- `OrderInfo`：order_id, side, size, price, status, filled_size, remaining_size, cancel_reason

**BaseExchangeClient(ABC)**：

- 构造：`__init__(self, config)`，config 实际为 `TradingConfig`；子类需实现 `_validate_config()`。
- **必须实现的抽象方法**：
  - `connect()` / `disconnect()`
  - `place_open_order(contract_id, quantity, direction)` → OrderResult
  - `place_close_order(contract_id, quantity, price, side)` → OrderResult
  - `cancel_order(order_id)` → OrderResult
  - `get_order_info(order_id)` → Optional[OrderInfo]
  - `get_active_orders(contract_id)` → List[OrderInfo]
  - `get_account_positions()` → Decimal
  - `setup_order_update_handler(handler)`（handler 为回调函数）
  - `get_exchange_name()` → str
- **已有实现**：`round_to_tick(price)` 使用 `config.tick_size` 做量化舍入。

**扩展方法（base 未声明，但 runbot/hedge 会调用，新交易所必须实现）**：

| 方法 | 签名 | 说明 |
|------|------|------|
| `get_contract_attributes` | `() -> Tuple[str, Decimal]` | 返回 (contract_id, tick_size)，一般根据 config.ticker 解析。 |
| `fetch_bbo_prices` | `(contract_id: str) -> Tuple[Decimal, Decimal]` | 返回 (best_bid, best_ask)。 |
| `get_order_price` | `(direction: str) -> Decimal` | 当前盘口价（buy 取 ask，sell 取 bid），用于等待/撤单逻辑。 |
| `place_market_order` | `(contract_id, quantity, direction) -> OrderResult` | 市价单；仅 Boost 及部分 hedge 使用；不支持可抛 NotImplementedError。 |

**WebSocket 订单更新回调约定**：  
handler 收到的 `message` 应为 dict，包含：`contract_id`、`order_id`、`status`（如 FILLED、CANCELED、PARTIALLY_FILLED）、`side`、`order_type`（OPEN/CLOSE）、`filled_size`、`size`、`price`。与 `trading_bot._setup_websocket_handlers` 内解析一致。

### 4.3 exchanges/factory.py

- `ExchangeFactory._registered_exchanges`：dict，exchange 名称（小写）→ 类路径字符串，例如 `'aster': 'exchanges.aster.AsterClient'`，懒加载。
- `ExchangeFactory.create_exchange(exchange_name, config)`：小写 name、动态 import、返回实例；不支持的 name 抛 ValueError。
- `ExchangeFactory.get_supported_exchanges()`：返回当前已注册名称列表（用于 runbot `--exchange` choices）。
- `ExchangeFactory.register_exchange(name, exchange_class)`：注册新交易所（校验继承 BaseExchangeClient）。

### 4.4 各交易所文件（exchanges/*.py）

| 文件 | 客户端类 | 说明 |
|------|----------|------|
| edgex.py | EdgeXClient | EdgeX SDK，Stark 私钥 |
| backpack.py | BackpackClient | bpx-py，BackpackWebSocketManager |
| paradex.py | ParadexClient | paradex_py，L2 私钥 |
| aster.py | AsterClient | REST + AsterWebSocketManager，listenKey 保活 |
| lighter.py | LighterClient | lighter-sdk，LighterCustomWebSocketManager |
| grvt.py | GrvtClient | grvt-pysdk |
| extended.py | ExtendedClient | x10-python-trading-starknet |
| apex.py | ApexClient | Apex SDK |
| nado.py | NadoClient | nado-python-sdk |
| standx.py | StandXClient | StandXAuth、StandXWebSocketManager |

每个客户端需实现 base 全部抽象方法及上述扩展方法（若参与 runbot/hedge）；WebSocket 在 `setup_order_update_handler` 中挂接，推送格式需符合 [4.2](#42-exchangesbasepy) 中的回调约定。

### 4.5 helpers

- **TradingLogger**（`helpers/logger.py`）：按 exchange、ticker、可选 ACCOUNT_NAME 在项目根下 `logs/` 建 activity log 与 orders CSV；`log(msg, level)`、`log_transaction(order_id, side, quantity, price, status)`；可配置 log_to_console、时区。
- **TelegramBot**（`helpers/telegram_bot.py`）：同步 `send_text(content)`，需 TELEGRAM_BOT_TOKEN、TELEGRAM_CHAT_ID。
- **LarkBot**（`helpers/lark_bot.py`）：异步 `send_text(content)`，需 LARK_TOKEN（飞书 Webhook）。

---

## 5. 数据流与调用关系（runbot）

1. **启动**：`runbot.py` 解析参数 → 加载 .env → 构造 `TradingConfig` → `ExchangeFactory.create_exchange(exchange, config)` → `TradingBot(config)`。
2. **TradingBot.run()**：`get_contract_attributes()` 得到 contract_id、tick_size → `connect()` → 主循环。
3. **主循环单轮**：`get_active_orders(contract_id)` → 过滤出平仓侧订单 → `_log_status_periodically()`（含仓位校验）→ `_check_price_condition()`（若 stop 则 shutdown）→ 若 pause 则 sleep 继续 → `_calculate_wait_time()`（若 >0 则 sleep 继续）→ `_meet_grid_step_condition()`（若不满足则 sleep 继续）→ `_place_and_monitor_open_order()`。
4. **下单与平仓**：`_place_and_monitor_open_order()` 内 `place_open_order()` → 若未即时成交则依赖 WebSocket 或轮询 `get_order_info()`；超时或价格不利则 `cancel_order()`；有成交则 `place_close_order()` 或 Boost 时 `place_market_order()`。价格判断用 `fetch_bbo_prices()`、`get_order_price()`。

---

## 6. 环境变量（键名速查）

- 通用：`ACCOUNT_NAME`、`TIMEZONE`（可选）。
- 通知：`TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`；`LARK_TOKEN`。
- 各交易所：EDGEX_*、BACKPACK_*、PARADEX_*、ASTER_*、LIGHTER_*（含 API_KEY_PRIVATE_KEY）、GRVT_*、EXTENDED_*、APEX_*、NADO_*。完整说明见根目录 README.md 与 env_example.txt。

---

## 7. 二次开发步骤（供 AI 执行）

### 7.1 新增 runbot 可用交易所

1. 在 `exchanges/` 下新增 `<name>.py`，实现类继承 `BaseExchangeClient`，实现全部抽象方法 + `get_contract_attributes`、`fetch_bbo_prices`、`get_order_price`；若支持 Boost 再实现 `place_market_order`。
2. 在 `exchanges/factory.py` 的 `_registered_exchanges` 中增加 `'<name>': 'exchanges.<name>.<Name>Client'`（类名与文件名对应）。
3. 可选：在 `exchanges/__init__.py` 的 `__all__` 中补充类名（factory 为懒加载，非必须）。
4. **详细步骤与完整示例代码见 `docs/ADDING_EXCHANGES.md`。**

### 7.2 新增对冲模式交易所

1. 在 `hedge/` 下新增 `hedge_mode_<name>.py`，实现与现有 hedge 模块一致的 `HedgeBot`（及可选 `Config`）；主交易所侧使用对应 `exchanges/<name>.py` 客户端，Lighter 侧使用 `LighterClient`。
2. 在 `hedge_mode.py` 的 `get_hedge_bot_class()` 与 `validate_exchange()` 中增加 `'<name>'` 分支。
3. 确保主交易所客户端已实现 `get_contract_attributes`、`fetch_bbo_prices`、下单/撤单等接口。

### 7.3 修改策略或 runbot 参数

- **策略逻辑**：只改 `trading_bot.py` 中 `TradingBot` 的主循环及 `_place_and_monitor_open_order`、`_handle_order_result` 等；修改等待条件、止盈计算、仓位校验在此完成。
- **新增 CLI 参数**：在 `runbot.py` 的 `parse_arguments()` 增加参数；在 `trading_bot.py` 的 `TradingConfig` 增加对应字段；在 `runbot.main()` 中把 args 传入 `TradingConfig`。若参数仅某交易所使用，可在该交易所客户端内读环境变量或 config。
- **日志与通知**：日志逻辑在 `helpers/logger.py`；通知入口在 `TradingBot.send_notification()`，可在此扩展更多渠道（不泄露 API Key/私钥）。

### 7.4 约定与约束（AI 必须遵守）

- **重试**：对易失败的 API 调用使用 `exchanges/base.py` 的 `query_retry`；参考 `tests/test_query_retry.py`。
- **代码风格**：项目根目录有 `.flake8`，修改时保持风格一致。
- **安全**：所有密钥、私钥仅从环境变量或 .env 读取，不写死、不提交到仓库。
- **接口兼容**：新增或修改交易所时，回调消息格式、OrderResult/OrderInfo 字段与本文档及 `docs/ADDING_EXCHANGES.md` 保持一致，避免破坏 `trading_bot._setup_websocket_handlers` 及 hedge 逻辑。

---

## 8. 文件与函数速查

| 文件 | 类/函数 | 用途 |
|------|---------|------|
| runbot.py | parse_arguments() | CLI 解析 |
| runbot.py | setup_logging() | 根 logger、抑制 websockets/urllib3/lighter 等 |
| runbot.py | main() | 加载 .env、构造 TradingConfig、创建 TradingBot、asyncio.run(bot.run()) |
| trading_bot.py | TradingConfig | 策略与合约参数容器 |
| trading_bot.py | TradingBot.run() | 主循环入口 |
| trading_bot.py | TradingBot._place_and_monitor_open_order() | 下单与监控 |
| trading_bot.py | TradingBot._handle_order_result() | 成交/撤单后平仓逻辑 |
| trading_bot.py | TradingBot._meet_grid_step_condition() | grid-step 判断 |
| trading_bot.py | TradingBot._check_price_condition() | stop/pause 价格判断 |
| exchanges/base.py | query_retry() | 重试装饰器 |
| exchanges/base.py | BaseExchangeClient | 抽象基类与 round_to_tick |
| exchanges/factory.py | ExchangeFactory.create_exchange() | 按名称创建交易所实例 |
| exchanges/factory.py | ExchangeFactory.get_supported_exchanges() | 当前支持的 exchange 列表 |
| hedge_mode.py | get_hedge_bot_class() | 按 exchange 返回 HedgeBot 类 |
| helpers/logger.py | TradingLogger.log(), log_transaction() | 日志与 CSV 记录 |

---

以上为代码库总结与 AI 二次开发所需的最小上下文；具体实现以仓库内源码为准。
