# Ostium + Lighter 对冲接入说明（主网）

本说明用于记录：Ostium 接入背景、标的对比脚本、运行方式与后续对冲模块开发计划。

## 背景与目标

- 目标：在 **Ostium 挂单（LIMIT maker）**，在 **Lighter 市价对冲（taker）**。
- 网络：**主网**。
- 数量单位：**基础资产数量**（与现有代码一致）。

## 参考文档

- `docs/ADDING_EXCHANGES.md`：新增交易所的接口约定与最佳实践（`BaseExchangeClient`、`OrderResult`、`OrderInfo`、注册流程）。
- 现有 hedge 模块：`hedge/hedge_mode_*.py`（参考现有 Lighter 逻辑）。
- Ostium 官方费率说明：[Fee Breakdown](https://ostium-labs.gitbook.io/ostium-docs/fee-breakdown)

## Ostium 费率机制（中文总结）

以下根据 [Ostium Fee Breakdown](https://ostium-labs.gitbook.io/ostium-docs/fee-breakdown) 整理，便于对冲模块开发时估算成本。

### 开仓费（Opening Fee）

- **一次性收取**，开仓时收取，**平仓不再另收**；传统资产为固定费率，结构简单。
- **加密货币**：按杠杆与 OI 倾斜决定 maker/taker
  - **Maker**：杠杆 ≤ 20× 且该笔交易减少 OI 失衡时适用，**3 bps**
  - **Taker**：杠杆 > 20× 或该笔交易增加 OI 失衡时适用，**10 bps**
  - 若部分平衡、部分加重失衡，则按比例分别计 maker / taker
- **非加密货币**：官方仅列出 **Taker** 费率（无 OI/杠杆调整）；**挂单 maker 可能为 0 费率**（文档未单独列出非 crypto 的 maker，通常理解为吃单收 taker、挂单 maker 不另收开仓费）。实际以 Ostium 前端/合约为准。

### 按资产类别的开仓费率

| 资产类别 | 开仓 | 平仓 |
|----------|------|------|
| 加密货币 | maker 3 bps / taker 10 bps | 0 |
| 指数 | taker 5 bps（**maker 可能 0**） | 0 |
| 外汇* | taker 3 bps（**maker 可能 0**） | 0 |
| 股票 | taker 5 bps（**maker 可能 0**） | 0 |

**例外**：USD/MXN 为 taker 5 bps。

**商品**（按标的；挂单 maker 可能 0，下表为 taker）：

| 标的 | 开仓 | 平仓 |
|------|------|------|
| XAU/USD | taker 3 bps | 0 |
| CL/USD | taker 10 bps | 0 |
| HG/USD | taker 15 bps | 0 |
| XAG/USD | taker 15 bps | 0 |
| XPT/USD、XPD/USD | taker 20 bps | 0 |

### Oracle 费（$0.10）

- 每次需要链上读取并 attest 外部价格时收取 **$0.10**。
- **开市价单**：若上链后失败（含滑点导致），手续费**不退还**。
- **下限价单**：下单时收取；**撤单**时仅退还抵押，Oracle 费**不退还**。
- **平仓**：先扣 $0.10，**若 100% 全平成功则退还**；若失败（如滑点）**不退还**。部分平仓、减仓等按次收取且不退还。

### 其他

- **清算费**：被清算时，剩余抵押作为负 PnL 归入 Vault。
- **价格影响**：无动态价差时按盘口 bid/ask 成交；启用 Dynamic Spreads 的标的按「价后影响」成交。
- **资金费率**（仅加密货币）：按 OI 多空失衡连续复利计费，平仓时结算，多空之间零和。
- **展期费**（非加密货币）：外汇/商品/指数/股票持仓按区块复利，平仓时结算，多空两侧费率可能不同。

## 标的重叠检测脚本

已新增脚本：

- `scripts/compare_ostium_lighter_markets.py`

功能：
- 拉取 Ostium 与 Lighter 的标的列表并归一化（`/`、`_` -> `-`，统一大写）。
- 输出**按基础资产**的交集（相同标的）与各自独有的标的列表（Ostium 多为 `XXX-USD`，与 Lighter 的 `XXX` 按 base 对比）。

### 运行方式

```bash
python scripts/compare_ostium_lighter_markets.py --network mainnet --rpc-url https://arb1.arbitrum.io/rpc
```

### 环境要求

- 需要安装 Ostium SDK：`pip install ostium-python-sdk`
- 必填：`RPC_URL`（也可通过参数 `--rpc-url` 传入）
- 可选：`PRIVATE_KEY`（不提供时为只读访问）

### 可配置参数

- `--network`：`mainnet` / `testnet`（默认 mainnet）
- `--rpc-url`：Ostium RPC URL
- `--private-key`：Ostium 私钥（可选）
- `--lighter-base-url`：Lighter REST base URL（默认 `https://mainnet.zklighter.elliot.ai`）
- `--timeout`：HTTP 超时（默认 15s）

## Ostium 与 Lighter 相同标的（主网）

脚本按「基础资产」+「货币对（6 位无连字符转 BASE-QUOTE）」对比，**相同标的总计 31 个**：24 个单一资产 + 7 个货币对。

### 单一资产（24 个）

| 基础资产 | Lighter 符号 | Ostium 符号 |
|----------|---------------|-------------|
| AAPL | AAPL | AAPL-USD |
| ADA | ADA | ADA-USD |
| AMZN | AMZN | AMZN-USD |
| BMNR | BMNR | BMNR-USD |
| BNB | BNB | BNB-USD |
| BTC | BTC | BTC-USD |
| COIN | COIN | COIN-USD |
| CRCL | CRCL | CRCL-USD |
| ETH | ETH | ETH-USD |
| HOOD | HOOD | HOOD-USD |
| HYPE | HYPE | HYPE-USD |
| LINK | LINK | LINK-USD |
| META | META | META-USD |
| MSFT | MSFT | MSFT-USD |
| MSTR | MSTR | MSTR-USD |
| NVDA | NVDA | NVDA-USD |
| PLTR | PLTR | PLTR-USD |
| SOL | SOL | SOL-USD |
| SPX | SPX | SPX-USD |
| TRX | TRX | TRX-USD |
| TSLA | TSLA | TSLA-USD |
| XAG | XAG | XAG-USD |
| XAU | XAU | XAU-USD |
| XRP | XRP | XRP-USD |

### 货币对（7 个）

Lighter 为 6 位无连字符（如 EURUSD、USDJPY），Ostium 为 BASE-QUOTE（如 EUR-USD、USD-JPY）；脚本已做归一化对比。

| Lighter 符号 | Ostium 符号 |
|--------------|-------------|
| AUDUSD | AUD-USD |
| EURUSD | EUR-USD |
| GBPUSD | GBP-USD |
| NZDUSD | NZD-USD |
| USDCAD | USD-CAD |
| USDCHF | USD-CHF |
| USDJPY | USD-JPY |

（Lighter 有 USDKRW，Ostium 当前无 USD-KRW，仅有 USD-MXN，故未计入。）

对冲模块开发时，可按上表做 Ostium pair 与 Lighter symbol 的映射。

## Ostium + Lighter 对冲策略约定

- **Ostium**：LIMIT maker 挂单；成交后触发 Lighter 对冲。
- **Lighter**：市价单立即对冲（taker）。
- **数量**：使用基础资产数量（与现有 hedge 逻辑一致）。

## 策略设计：低手续费 + 价差利用 + 综合考量

在满足「Ostium 挂 maker、Lighter 市价对冲」的前提下，从费率、价差和风险三方面给出设计建议，便于实现时做参数与逻辑取舍。

### 一、手续费尽量压低

| 环节 | 建议 | 说明 |
|------|------|------|
| **Ostium 开仓** | 尽量拿 **maker**（开仓费更低或为 0） | **非加密货币**：官方仅列 taker，挂单 maker **可能 0 费率**，外汇/指数/股票/商品若挂单成交则 Ostium 侧开仓费可视为 0。**加密货币**：maker 3 bps（杠杆 ≤ 20× 且减 OI 失衡）、否则 taker 10 bps。 |
| **Ostium 撤单** | **少撤单** | 下限价单时已扣 Oracle $0.10，**撤单不退还**。挂单价格尽量一次设准，或设较长超时再撤，避免频繁挂/撤。 |
| **Ostium 平仓** | **100% 全平、确保成功** | 全平成功会**退还** $0.10；失败（如滑点）不退还。避免部分平、减仓（每次扣 $0.10 且不退）。平仓前可对比 Lighter 盘口，滑点过大时暂不平或调小量。 |
| **Lighter** | 对冲用市价，无法省 taker | 为速度必须吃 taker；可接受滑点范围内尽量用限价/市价限价减少冲击。 |
| **标的选择** | 优先非 crypto 拿 maker（可能 0）或 crypto maker | 外汇/指数/股票/商品：挂单 maker 可能 **0 费**，吃单才收 taker；加密货币若挂单「平衡 OI」则 3 bps maker。 |

### 二、用价差赚钱：何时挂单、挂什么价

两边同一标的价格常有细微差异，可用「Ostium 成交价 vs Lighter 对冲成交价」的差覆盖手续费并赚一点。

- **做多一轮**：Ostium **限价买** 成交价 `P_ostium_buy` → 立刻在 Lighter **市价卖**，约在 Lighter 的 bid `P_lighter_bid` 成交。  
  - **单位毛利** ≈ `P_lighter_bid - P_ostium_buy`，再减去两边开仓手续费（及若立刻平仓的 Ostium Oracle 退还前的净成本、Lighter 平仓费）。
- **做空一轮**：Ostium **限价卖** 成交价 `P_ostium_sell` → 立刻在 Lighter **市价买**，约在 Lighter 的 ask `P_lighter_ask` 成交。  
  - **单位毛利** ≈ `P_ostium_sell - P_lighter_ask`，再减手续费。

**设计要点：**

1. **下单前校验价差**  
   - 做多：只有 `Lighter_bid - 拟挂 Ostium 买价 > 预估单位总成本` 才挂 Ostium 买单；做空：只有 `拟挂 Ostium 卖价 - Lighter_ask > 预估单位总成本` 才挂 Ostium 卖单。  
   - 总成本 = Ostium 开仓费（3～10 bps 按标的/方向）+ Lighter 开仓 taker + 若本轮回平则两边平仓费 + Oracle 净成本（成功全平则 Ostium 退 $0.10，否则按实际发生算）。

2. **挂单价格**  
   - 做多：Ostium 买单价要**低于**当前 Lighter bid，这样成交后到 Lighter 卖能落在 bid 附近，价差才存在；做空：Ostium 卖单价要**高于**当前 Lighter ask。  
   - 可设「最小价差 bps」参数：只有 (Lighter_bid - Ostium_buy) 或 (Ostium_sell - Lighter_ask) 超过该 bps 才挂单，避免价差被手续费吃光。

3. **实现上**  
   - 轮询或订阅两边的 BBO（best bid/offer），算出上述不等式，满足再发 Ostium 限价单；Ostium 成交后立刻发 Lighter 市价对冲。  
   - 若希望「价差缩小」也赚钱：例如先 Ostium 买 + Lighter 卖锁定价差，等两边价差收窄或反转时再 Ostium 全平 + Lighter 市价平，此时平仓端也能吃一点价差；前提是平仓时再次用 BBO 判断，且全平成功以拿回 $0.10。

### 三、其他考量

| 项目 | 建议 |
|------|------|
| **Oracle $0.10 摊薄** | 名义额不宜过小，否则单笔 $0.10 占比高。可设最小单笔名义额或最小 bps 利润，低于则不下单。 |
| **滑点** | Lighter 市价单设最大滑点（或限价）；Ostium 全平前看 Lighter 盘口，滑点过大可延后平仓或减量，避免平仓失败损失 $0.10。 |
| **资金费率 / 展期** | 加密货币有资金费率，非加密货币有展期费。若不做持仓套利，尽量「开仓→对冲→尽快双平」，缩短持仓时间。 |
| **流动性** | Ostium 挂单太远不易成交，太近价差小。可用「相对 Lighter mid 的偏移 bps」或「相对 Ostium 当前 BBO 的偏移」控制挂单距离，兼顾成交率与价差。 |
| **标的与时段** | 外汇、XAU 等 taker 3 bps 的标的单位成本低；流动性好的时段价差更稳定，便于稳定吃到价差。 |

### 四、小结（实现时可调参数）

- **费率侧**：Ostium 能 maker 就 maker；少撤单；平仓只做 100% 全平并保证成功以退 Oracle。
- **价差侧**：仅当「预期价差 > 单位总成本」时挂单；挂单价相对 Lighter BBO 要有利（买低于 Lighter bid、卖高于 Lighter ask）；可选「最小价差 bps」过滤。
- **风控与成本**：最小名义额、最大滑点、持仓时间尽量短；标的优先低 taker 或能拿 maker 的。

以上可作为 `hedge_mode_ostium` 中挂单条件、价格计算与平仓逻辑的设计依据，具体阈值（最小价差、最小名义额、滑点上限等）可按实盘数据再调。

## Ostium 对冲模块参数（环境变量）

- `PRIVATE_KEY`：Ostium 交易私钥（必填）
- `RPC_URL`：Arbitrum RPC（必填）
- `OSTIUM_LEVERAGE`：杠杆倍数（默认 5）
- `OSTIUM_PRICE_OFFSET_BPS`：LIMIT maker 价格偏移（bps，默认 5）
- `LIGHTER_ACCOUNT_INDEX`：Lighter 账户索引
- `LIGHTER_API_KEY_INDEX`：Lighter API key 索引
- `API_KEY_PRIVATE_KEY`：Lighter API 私钥
- `LIGHTER_BASE_URL`：Lighter REST base URL（默认 `https://mainnet.zklighter.elliot.ai`）

## 运行方式

```bash
python hedge_mode.py --exchange ostium --ticker BTC --size 0.001 --iter 10 --fill-timeout 10 --sleep 0 --max-position 0.01
```

## 监控指标规范（Ostium / Lighter）

> 当前仅覆盖 Ostium/Lighter，后续可扩展到更多交易所。

### 1) 买卖价差（Bid-Ask Spread）

**目的**：衡量单平台流动性与真实成交成本。

**计算方式**：
- 传统：`spread = (ask - bid) / bid × 100%`
- 建议：用 orderbook 前 5 档或 10 档的**加权平均价**估算真实可成交价：
  - `vwap_bid = Σ(bid_price_i × bid_size_i) / Σ(bid_size_i)`
  - `vwap_ask = Σ(ask_price_i × ask_size_i) / Σ(ask_size_i)`
  - `spread_vwap = (vwap_ask - vwap_bid) / vwap_bid × 100%`

**规则**：
- 高 spread 资产要自动过滤，或提高预警阈值。
- 建议**至少 10,000 美金深度**的 VWAP 价参与计算，以避免盘口虚假价差。
 - **美股交易时段限制**：Ostium 提供 `isMarketOpen` / `isDayTradingClosed`，监控与执行需过滤非交易时段（若接口不可用则使用固定交易时段配置）。

### 2) 跨交易所价格差（同一资产）

**目的**：判断是否存在可套利方向。

**计算方式**（双向）：
- 方向 A→B：`diff_ab = (bid_A - ask_B) / ask_B × 100%`
- 方向 B→A：`diff_ba = (bid_B - ask_A) / ask_A × 100%`
- 取最大可套利方向：`max(diff_ab, diff_ba)`

**净收益需扣除**：
- 双边 taker 手续费
- 双边 spread（真实成交价差）
- 提币/链上转移费用
- 预计转移时间内的波动风险缓冲

### 规则优化与默认推荐值（监控）

以下为当前监控脚本建议的默认值（可按实盘微调）：

| 规则 | 默认值 | 说明 |
|------|--------|------|
| VWAP 深度 | 10,000 美金 | Lighter 侧使用 $10k 深度 VWAP 作为买/卖价 |
| 最小深度 | 10,000 美金 | 任一侧深度不足则剔除 |
| max_spread_bps | 50 | 盘口 spread 超过即过滤 |
| spread_weight | 0.2 | 动态阈值 = min_net_bps + spread × weight |
| max_dislocation_bps | 500 | 防止异常价差/数据毛刺 |
| min_net_bps | 0.01 | 基础最小净利阈值 |

**动态阈值**：
`min_net_bps + spread_bps × spread_weight`

### 3) 永续合约资金费率

**数据点**：
- 当前 funding rate（实时值）
- 预测 funding rate（下一期预测值）

**费率差**：
- `rate_diff = funding_A - funding_B`

**年化换算**（按收取周期）：
- `annualized = rate_diff × (365 × 24 / 收取间隔小时数) × 100%`

### 4) 资金费率收取时间点与间隔

**记录字段**：
- 每个交易所的收取周期（常见 8 小时）
- 下一次收取的 UTC 时间戳

**策略提示**：
- 重点关注“时间错位”机会：例如 A 平台 00:00 收，B 平台 01:00 收，中间 1 小时窗口可调整仓位。

---

## 套利脚本规划（基于监控逻辑）

目标：在满足“低风险 + 高净利”的条件下自动执行 Ostium/Lighter 对冲套利。

### 执行前置条件（风控）

- **市场状态**：股票类标的必须 `isMarketOpen=True` 且 `isDayTradingClosed=False`。
- **深度要求**：Lighter 侧 VWAP 需满足 $10,000 深度（可配置）。
- **价差与净利**：`净利 >= 动态阈值` 且 `净利 >= alert_net_bps`。
- **异常过滤**：盘口 spread 超限、价差异常（dislocation）直接跳过。

### 执行步骤（单轮）

1) 拉取 Ostium 价格与市场状态（含 `isMarketOpen`）。
2) 拉取 Lighter 订单簿并计算 $10k VWAP bid/ask。
3) 计算：
   - 点差毛利（Ostium 限价 vs Lighter VWAP）
   - 综合成本（Ostium 开仓费 + Oracle fee + buffer）
   - 资金费率收益/成本（按方向）
   - 净利（含资金）
4) 若通过过滤条件：
   - Ostium 下 LIMIT 单（maker）
   - 成交后 Lighter 市价对冲
   - 记录订单、价格、净利
5) 若未成交超时：撤单并记录（注意 Oracle fee 不退）。

### 参数化（建议默认）

- `DEPTH_QUOTE_USD=10000`
- `MIN_NET_BPS=0.01`
- `MAX_SPREAD_BPS=50`
- `SPREAD_WEIGHT=0.2`
- `MAX_DISLOCATION_BPS=500`
- `ORDER_TIMEOUT_SEC=10`

---
## 费率与策略设计（开发前准备）

### Ostium 费用要点（摘要）

- **开仓费（Opening Fee）**：非加密资产为固定费率；加密资产按杠杆/持仓不平衡方向决定 maker 或 taker 费率。citeturn0view0
- **加密资产费率**：maker 3 bps、taker 10 bps；**收取在开仓**，平仓为 0。citeturn0view0
- **非加密资产费率**：官方说明为“静态 taker fee”，指数 5 bps、外汇 3 bps、股票 5 bps；**收取在开仓**，平仓为 0。文档未给出 maker 费率（意味着非加密不区分 maker/taker）。citeturn0search0
- **例外与商品**：如 XAU/USD 3 bps、XAG/USD 15 bps 等（均为开仓费）。citeturn0view0
- **Oracle Fee**：开仓/挂单等动作会有 $0.10 的预付费用，部分场景不退。citeturn0view0
- **Funding / Rollover**：加密资产有 funding，非加密资产有 rollover，均按区块累计并在平仓结算。citeturn0view0

### 设计假设（基于你的输入）

- Lighter maker/taker 费率为 0（当前设定）
- 最小净利阈值：**0.01 bps**
- RPC 成本忽略
- 交易风格：**低频大利**

### 策略设计原则（用于下一步开发）

1) **只做“价差 > 总成本 + buffer”**  
   以 Ostium 的开仓费 + Oracle Fee + 预计 funding/rollover 为主要成本；  
   Lighter 费用为 0，仍需考虑滑点与执行偏差。

2) **低频大利 => 更严格的触发阈值**  
   用“动态阈值”替代固定 0.01 bps：  
   - 资产属于 crypto：按 maker/taker 费率区分  
   - 资产属于非 crypto：按固定开仓费率  
   - 再加：Oracle Fee 的摊销、预计 funding/rollover

3) **Ostium 优先 maker（降低开仓费）**  
   对 crypto 资产：尽量保证 maker 条件（杠杆 <= 20x 且有助于 OI 反向），降低至 3 bps。citeturn0view0  
   否则按 taker 10 bps 计算，触发阈值应明显抬高。

4) **把 Oracle Fee 显式计入成本模型**  
   特别是限价挂单被撤销或超时的场景，Oracle Fee 不退。citeturn0view0  
   => 低频策略要降低“挂单后撤单”的比例。

5) **只在“价差持续性”更强的标的上做**  
   对冲标的优先级（建议）：  
   - crypto（BTC/ETH/SOL 等）：考虑 funding 与流动性  
   - 外汇（EURUSD/GBPUSD 等）：rollover 成本与交易时段  
   - 股票/指数（AAPL/SPX 等）：关注 Ostium “日内/闭市”状态

### 建议的参数化模型（草案）

- `spread_min_bps`：基础最小利润阈值（>= 0.01 bps）
- `cost_open_bps`：按资产类型/是否 maker 取 Ostium 开仓费（bps）
- `oracle_fee_usd`：$0.10，按订单大小换算成 bps
- `funding_bps_est` / `rollover_bps_est`：根据持仓预期时长估计
- `buffer_bps`：风险缓冲（滑点、执行偏差）

触发条件示例：

```
spread_bps >= cost_open_bps
            + oracle_fee_bps
            + funding_or_rollover_bps
            + buffer_bps
```

### 对冲执行优化（低频策略）

- **挂单频率控制**：减少“挂单 → 撤单”循环，避免多次支付 Oracle Fee。citeturn0view0
- **成交后对冲**：Ostium 成交后立即 Lighter 市价对冲，降低净敞口风险。
- **单次订单规模更大**：符合“低频大利”策略预期，同时摊薄 Oracle Fee。

## 对冲模块实现状态

1) 已新增对冲模式模块：`hedge/hedge_mode_ostium.py`
   - Ostium 侧：
     - 通过 SDK/subgraph 获取 `pair_id` / 标的信息
     - LIMIT 挂单 → 轮询/事件检测成交 → 触发 Lighter 市价对冲
   - Lighter 侧：复用现有 hedge 模块的 market/taker 逻辑

2) 已接入入口：`hedge_mode.py`
   - `validate_exchange()`、`get_hedge_bot_class()` 增加 `ostium`

3) 可选：如需 runbot 通用策略，再新增 `exchanges/ostium.py`
   - 继承 `BaseExchangeClient`
   - 实现 `get_contract_attributes` / `fetch_bbo_prices` / `get_order_price`
   - 在 `exchanges/factory.py` 注册

## 待补充信息

- Ostium ↔ Lighter 标的映射见上表「Ostium 与 Lighter 相同标的」；后续若有新增可再跑脚本更新。
- Ostium 具体下单参数（例如是否使用固定 leverage / collateral）
- 成交检测与超时/撤单策略

---

如需开始写对冲模块，请提供目标标的清单或直接确认脚本结果。
