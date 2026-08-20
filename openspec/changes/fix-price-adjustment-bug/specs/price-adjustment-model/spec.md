# price-adjustment-model Specification

## Purpose
定义日线行情的复权数据模型、前复权价格计算算法和回测价格使用规范，解决当前 daily_bar 表混存前复权与不复权价格导致回测收益虚高的问题。

## Background

### 缺陷根因

当前 `daily_bar` 表混存了三种数据源的价格，复权基准不同且无 `adjust_type` 字段区分：

| 数据源 | 行数 | 日期范围 | 复权类型 |
|--------|------|----------|----------|
| baidu | ~5.5M | 2013 ~ 2026-06 | 前复权（有基线漂移） |
| ths | ~92K | 2026-05 ~ 2026-07 | 不复权 |
| tdxquant | ~81K | 2026-07 ~ 2026-08 | 不复权 |

`load_price_cache()` 直接 `SELECT open_price, close_price FROM daily_bar`，不区分来源。两个问题叠加导致回测收益虚高：

1. **baidu 前复权基线漂移**：不同批次拉取的前复权价格用了不同基准日，同一股票相邻两天的价格比被扭曲。
2. **数据源切换边界跳价**：baidu（前复权）到 ths/tdxquant（不复权）切换时，价格跳变制造虚假收益。

### 前复权算法验证结论

通过 TDXQuant 接口验证，前复权不是简单的 `价格 × 比例因子`，而是一个**线性变换**：

```
前复权价 = A × 原始价 + B
```

其中 A 和 B 通过倒序遍历除权除息事件计算：

- `m = (10 + ShareBonus + Allotment) / 10` — 股本变化比
- `c = (Bonus - Allotment × AllotPrice) / 10` — 现金偏移
- `A = A / m` — 先更新 A
- `B = B - A × c` — 用更新后的 A

初始值 `A=1, B=0`，从最新事件向最旧事件倒序遍历。

验证结果：对宇通客车（600066.SH）全量 7010 个交易日，用此公式从 `get_divid_factors()` 推导的前复权价格与 API `dividend_type='front'` 返回的价格**全部匹配**，最大误差 0.005（四舍五入精度）。

## Requirements

### Requirement: daily_bar 必须存储不复权 OHLC 和前复权 OHLC

系统 MUST 在 `daily_bar` 表中同时存储不复权和前复权两套 OHLC 价格。不复权价格用于真实成交模拟，前复权价格用于技术指标计算（MA、MACD 等）。前复权价格 MUST 在入库时预计算并持久化，回测查询时直接读取，不在运行时重复计算。

新增字段：

```
open_qfq   DECIMAL(18,4)
high_qfq   DECIMAL(18,4)
low_qfq    DECIMAL(18,4)
close_qfq  DECIMAL(18,4)
```

#### Scenario: 前复权价格可直接查询用于指标计算
- **WHEN** 策略需要计算 MA20
- **THEN** 系统 MUST 能直接 `SELECT close_qfq FROM daily_bar` 并执行 `rolling(20).mean()`，无需运行时复权计算

#### Scenario: 不复权价格用于真实成交模拟
- **WHEN** 回测引擎计算买入/卖出成交金额
- **THEN** 系统 MUST 使用 `open_price` / `close_price`（不复权），而不是 `open_qfq` / `close_qfq`

### Requirement: event_record 必须存储除权除息事件

系统 MUST 将 `get_divid_factors()` 返回的除权除息事件存储到 `event_record` 表。每个事件 MUST 包含证券标识、事件日期、事件类型、现金分红、送股比例、配股比例和配股价。

`event_type` MUST 使用 `dividend` 标识除权除息事件。`event_payload` MUST 存储 JSON 格式的完整事件参数（Bonus、ShareBonus、Allotment、AllotPrice）。

#### Scenario: 除权除息事件可按证券和日期查询
- **WHEN** 前复权计算需要某只股票的除权除息事件
- **THEN** 系统 MUST 能按 `security_id` 查询到该股票的全部除权除息事件，并按日期排序

#### Scenario: 同一证券的多次除权除息事件都被保留
- **WHEN** 一只股票在历史上有 34 次除权除息
- **THEN** 系统 MUST 在 `event_record` 中保留 34 条记录，不覆盖、不丢失

### Requirement: 前复权价格必须通过线性变换公式计算

系统 MUST 使用线性变换公式 `QFQ = A × Price + B` 计算前复权价格，而不是简单的比例乘法 `Price × ForwardFactor`。A 和 B MUST 通过倒序遍历除权除息事件（从最新到最旧）计算，初始 `A=1, B=0`。

#### Scenario: 纯现金分红事件只改变 B 不改变 A
- **WHEN** 某次除权除息事件为 10派10（ShareBonus=0, Allotment=0）
- **THEN** m=1, A 不变，B 减少 1.0（每股分红 1 元）

#### Scenario: 送股事件改变 A
- **WHEN** 某次除权除息事件为 10送8（Bonus=0, ShareBonus=8, Allotment=0）
- **THEN** m=1.8, A 变为 A/1.8，B 也相应调整

#### Scenario: 配股事件同时改变 A 和 B
- **WHEN** 某次除权除息事件为 10配3（Allotment=3, AllotPrice=10）
- **THEN** m=1.3, c=(0-3×10)/10=-3, A 变为 A/1.3, B 变为 B - A_new × (-3)

### Requirement: daily_bar 必须新增 adjust_type 字段标记复权基准

系统 MUST 在 `daily_bar` 表中新增 `adjust_type` 字段，标记每行价格的复权基准。不复权数据 MUST 标记为 `none`，前复权数据 MUST 标记为 `front`。

#### Scenario: 回测可按复权类型过滤数据
- **WHEN** 回测引擎加载价格数据
- **THEN** 系统 MUST 能通过 `adjust_type` 区分不复权和前复权数据，避免混用不同复权基准的价格

### Requirement: 数据源必须统一使用 TDXQuant 不复权价格

系统 MUST 将 TDXQuant（`dividend_type='none'`）作为日线行情的唯一数据源，替代 baidu 前复权数据。baidu 数据源 MUST 被标记为弃用，不再写入新数据。

#### Scenario: 新数据只来自 TDXQuant 不复权
- **WHEN** 系统执行增量数据导入
- **THEN** 系统 MUST 使用 TDXQuant `dividend_type='none'` 获取不复权价格，并计算前复权 OHLC 后写入 daily_bar

#### Scenario: baidu 前复权数据被清除
- **WHEN** 系统执行数据修复
- **THEN** 系统 MUST 将 baidu 来源的 daily_bar 记录替换为 TDXQuant 不复权价格 + 前复权 OHLC

### Requirement: 前复权计算必须使用全量除权除息事件

系统 MUST 使用全量除权除息事件（`count=-1`）计算前复权价格。前复权价格的基准是最新交易日，因此 MUST 包含从股票上市到最新交易日的所有除权除息事件。

#### Scenario: 限定日期范围的事件导致前复权错误
- **WHEN** 系统只拉取部分日期范围的除权除息事件
- **THEN** 计算出的前复权价格 MUST 被视为不可靠，系统 MUST 要求全量拉取

### Requirement: 回测引擎必须区分指标价格和成交价格

系统 MUST 在回测引擎中区分两类价格用途：
- **指标计算**（MA、MACD、选股信号）：MUST 使用前复权价格（`close_qfq`、`open_qfq`、`high_qfq`、`low_qfq`）
- **成交模拟**（买入金额、卖出金额、持仓市值、止盈止损判断）：MUST 使用不复权价格（`open_price`、`close_price`、`low_price`）

#### Scenario: 选股信号使用前复权价格计算
- **WHEN** 选股策略计算 MA5/MA10/MA20 均线
- **THEN** 系统 MUST 使用 `close_qfq` 计算均线，确保除权除息不影响技术指标形态

#### Scenario: 买入金额使用不复权价格
- **WHEN** 回测引擎计算买入金额 = 买入价 × 持股数
- **THEN** 系统 MUST 使用 `open_price`（不复权开盘价）作为买入价

#### Scenario: 止损判断使用不复权价格
- **WHEN** 回测引擎判断当日最低价是否触发止损
- **THEN** 系统 MUST 使用 `low_price`（不复权最低价）与买入价（不复权）比较

### Requirement: load_price_cache 必须返回不复权价格

系统 MUST 保持 `load_price_cache()` 返回不复权价格（`open_price`, `close_price`），用于成交模拟。系统 MUST 新增 `load_qfq_price_cache()` 返回前复权价格（`open_qfq`, `close_qfq`），用于指标计算。

#### Scenario: 成交模拟读取不复权价格
- **WHEN** `run_loaded_strategy_backtest()` 执行买入/卖出
- **THEN** `load_price_cache()` MUST 返回不复权 `open_price` 和 `close_price`

#### Scenario: 指标计算读取前复权价格
- **WHEN** 选股策略计算技术指标
- **THEN** `load_qfq_price_cache()` MUST 返回前复权 `open_qfq` 和 `close_qfq`

### Requirement: load_low_price_cache 必须返回不复权最低价

系统 MUST 保持 `load_low_price_cache()` 返回不复权 `low_price`，用于止损判断。前复权最低价（`low_qfq`）MUST 通过新增的 `load_low_qfq_cache()` 提供，供需要前复权最低价的场景使用。

#### Scenario: 止损使用不复权最低价
- **WHEN** `intraday_stop_loss_enabled` 为 True
- **THEN** `load_low_price_cache()` MUST 返回不复权 `low_price`

### Requirement: 系统必须提供数据回填命令

系统 MUST 提供一个命令行入口，用于用 TDXQuant 不复权数据替换 daily_bar 中的 baidu 前复权数据，并计算前复权 OHLC。该命令 SHALL 支持按股票代码、日期范围或全量回填。

#### Scenario: 全量回填
- **WHEN** 用户执行全量回填命令
- **THEN** 系统 MUST 遍历所有股票，用 TDXQuant 不复权价格替换 daily_bar 中的 baidu 数据，并计算写入前复权 OHLC

#### Scenario: 单股票回填
- **WHEN** 用户指定股票代码执行回填
- **THEN** 系统 MUST 只处理该股票，并输出处理行数摘要

### Requirement: 前复权价格必须在数据导入时预计算

系统 MUST 在数据导入流程（`ingest-tdxquant`）中预计算前复权 OHLC 并写入 `daily_bar`。预计算流程为：拉取不复权日线 → 拉取全量除权除息事件 → 计算线性变换 A/B → 计算 `open_qfq = A × open_price + B` 等 → 写入 daily_bar。

#### Scenario: 导入时自动计算前复权
- **WHEN** 用户执行 `quant ingest-tdxquant`
- **THEN** 系统 MUST 在写入不复权 OHLC 的同时计算并写入前复权 OHLC

#### Scenario: 除权除息事件也被写入 event_record
- **WHEN** 数据导入完成
- **THEN** 系统 MUST 将除权除息事件写入 `event_record` 表，供后续前复权重算使用
