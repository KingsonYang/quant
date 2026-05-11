# A 股日线回测系统第一版数据模型设计

## 1. 目标

本文档定义第一版 A 股日线回测系统的数据模型与表结构，目标如下：

- 统一原始抓取、标准化行情、回测结果三类数据的边界
- 为后续 `collector`、`parser`、`storage`、`engine`、`strategy`、`analytics` 模块提供统一数据契约
- 支持当前百度日 K 数据来源的落地
- 支持日线级回测、收盘信号、次日成交、T+1 与基础交易约束
- 兼容后续股票池、多策略、多次回测任务扩展

## 2. 数据域分层

系统采用三层数据域：

### 2.1 原始层 Raw

职责：

- 保存原始请求和原始响应
- 保存抓取时间、请求参数、解析状态
- 支持重放、审计、字段变更排查

该层不直接提供给策略使用。

### 2.2 标准层 Standard

职责：

- 保存标准化后的证券主数据
- 保存交易日历
- 保存日线行情与事件数据
- 作为策略和回测的统一数据输入层

该层是回测引擎主要读取的数据层。

### 2.3 结果层 Result

职责：

- 保存策略定义和回测任务
- 保存订单、成交、持仓、每日权益
- 支持结果复现、归因分析和交易分析

## 3. 命名约定与主键策略

### 3.1 命名约定

- 表名统一使用小写蛇形：`security_master`
- 字段名统一使用小写蛇形：`trade_date`
- 日期字段优先用业务语义：`trade_date`、`event_date`、`submitted_date`
- 时间戳字段统一以 `_at` 结尾：`created_at`、`updated_at`、`fetched_at`
- 布尔字段统一以 `is_` 开头：`is_trading_day`
- 代码字段使用 `symbol` 表示外部证券代码，格式推荐为 `SZ000001`

### 3.2 主键策略

- 内部实体一律使用稳定主键 `*_id`
- 与市场数据相关的事实表优先使用“实体主键 + 业务日期”联合唯一键
- 对外展示、导入导出保留 `symbol`
- 内部表关联优先使用 `security_id`、`strategy_id`、`run_id` 等内部主键

### 3.3 核心实体

- `security_id`：证券内部主键
- `calendar_id`：交易日历主键
- `raw_request_id`：原始请求主键
- `raw_response_id`：原始响应主键
- `strategy_id`：策略定义主键
- `run_id`：回测任务主键
- `order_id`：订单主键
- `fill_id`：成交主键
- `position_snapshot_id`：持仓快照主键
- `equity_snapshot_id`：权益快照主键

## 4. 第一版必须实现的核心表

### 4.1 `security_master`

用途：证券主数据。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| security_id | BIGINT | 内部主键 |
| symbol | VARCHAR | 外部代码，如 `SZ000001` |
| exchange | VARCHAR | 交易所，如 `SZ`、`SH`、`BJ` |
| code | VARCHAR | 纯数字代码，如 `000001` |
| name | VARCHAR | 证券名称 |
| market | VARCHAR | 市场类型，如 `ab` |
| board | VARCHAR | 板块，如 `main`、`gem`、`star` |
| list_status | VARCHAR | 上市状态，如 `listed`、`delisted` |
| list_date | DATE | 上市日期 |
| delist_date | DATE | 退市日期，可空 |
| currency | VARCHAR | 币种，默认 `CNY` |
| data_source | VARCHAR | 主数据来源 |
| created_at | TIMESTAMP | 创建时间 |
| updated_at | TIMESTAMP | 更新时间 |

主键与约束：

- PK：`security_id`
- UK：`symbol`
- UK：`(exchange, code)`

### 4.2 `trading_calendar`

用途：统一交易日历。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| calendar_id | BIGINT | 主键 |
| market | VARCHAR | 市场，如 `ab` |
| trade_date | DATE | 自然日期 |
| is_trading_day | BOOLEAN | 是否交易日 |
| prev_trade_date | DATE | 前一交易日 |
| next_trade_date | DATE | 下一交易日 |
| created_at | TIMESTAMP | 创建时间 |
| updated_at | TIMESTAMP | 更新时间 |

主键与约束：

- PK：`calendar_id`
- UK：`(market, trade_date)`

### 4.3 `daily_bar`

用途：标准化日线行情。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| security_id | BIGINT | 证券主键 |
| trade_date | DATE | 交易日 |
| ts_epoch | BIGINT | 原始时间戳 |
| open_price | DECIMAL(18,4) | 开盘价 |
| high_price | DECIMAL(18,4) | 最高价 |
| low_price | DECIMAL(18,4) | 最低价 |
| close_price | DECIMAL(18,4) | 收盘价 |
| pre_close_price | DECIMAL(18,4) | 昨收 |
| volume | BIGINT | 成交量 |
| amount | DECIMAL(20,2) | 成交额 |
| change_amount | DECIMAL(18,4) | 涨跌额 |
| change_pct | DECIMAL(10,4) | 涨跌幅 |
| turnover_ratio | DECIMAL(10,4) | 换手率 |
| ma5_price | DECIMAL(18,4) | ma5 均价，可空 |
| ma10_price | DECIMAL(18,4) | ma10 均价，可空 |
| ma20_price | DECIMAL(18,4) | ma20 均价，可空 |
| ma5_volume | BIGINT | ma5 成交量，可空 |
| ma10_volume | BIGINT | ma10 成交量，可空 |
| ma20_volume | BIGINT | ma20 成交量，可空 |
| source_name | VARCHAR | 来源名称 |
| parser_version | VARCHAR | 解析版本 |
| ingested_at | TIMESTAMP | 标准化入库时间 |

主键与约束：

- PK：逻辑主键 `(`security_id`, `trade_date`)`
- FK：`security_id -> security_master.security_id`

### 4.4 `event_record`

用途：财报发布等事件数据。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| event_id | BIGINT | 主键 |
| security_id | BIGINT | 证券主键 |
| event_date | DATE | 事件日期 |
| event_type | VARCHAR | 事件类型，如 `financial_report` |
| event_title | VARCHAR | 事件标题 |
| event_content | TEXT | 事件文本 |
| event_payload | TEXT | 原始载荷 JSON/文本 |
| source_name | VARCHAR | 数据来源 |
| created_at | TIMESTAMP | 创建时间 |

主键与约束：

- PK：`event_id`
- IDX：`(security_id, event_date)`

### 4.5 `raw_market_request`

用途：原始请求记录。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| raw_request_id | BIGINT | 主键 |
| security_id | BIGINT | 证券主键，可空 |
| source_name | VARCHAR | 数据来源 |
| endpoint | VARCHAR | 请求地址 |
| http_method | VARCHAR | 请求方法 |
| query_params | TEXT | 查询参数 JSON |
| requested_at | TIMESTAMP | 请求时间 |
| request_status | VARCHAR | 请求状态 |
| error_message | TEXT | 错误信息 |

### 4.6 `raw_market_response`

用途：原始响应记录。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| raw_response_id | BIGINT | 主键 |
| raw_request_id | BIGINT | 对应请求 |
| security_id | BIGINT | 证券主键，可空 |
| fetched_at | TIMESTAMP | 抓取完成时间 |
| response_format | VARCHAR | 如 `jsonp`、`json` |
| response_body | TEXT | 原始响应体 |
| parse_status | VARCHAR | 解析状态 |
| parser_version | VARCHAR | 解析器版本 |
| checksum | VARCHAR | 内容校验值 |

### 4.7 `strategy_definition`

用途：策略定义。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| strategy_id | BIGINT | 主键 |
| strategy_name | VARCHAR | 策略名称 |
| strategy_key | VARCHAR | 稳定唯一标识 |
| strategy_type | VARCHAR | 策略类型 |
| description | TEXT | 描述 |
| parameter_schema | TEXT | 参数结构 JSON |
| version | VARCHAR | 版本 |
| created_at | TIMESTAMP | 创建时间 |
| updated_at | TIMESTAMP | 更新时间 |

### 4.8 `backtest_run`

用途：一次回测运行。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| run_id | BIGINT | 主键 |
| strategy_id | BIGINT | 策略主键 |
| run_kind | VARCHAR | 运行类型，如 `selection_backtest`、`strategy_backtest` |
| run_name | VARCHAR | 回测任务名称 |
| start_date | DATE | 起始日期 |
| end_date | DATE | 结束日期 |
| benchmark_symbol | VARCHAR | 基准代码，可空 |
| universe_definition | TEXT | 股票池定义 JSON |
| parameter_snapshot | TEXT | 参数快照 JSON |
| initial_cash | DECIMAL(20,2) | 初始资金 |
| commission_rate | DECIMAL(10,6) | 手续费率 |
| tax_rate | DECIMAL(10,6) | 印花税率 |
| slippage_rate | DECIMAL(10,6) | 滑点率 |
| run_status | VARCHAR | 任务状态 |
| started_at | TIMESTAMP | 开始时间 |
| finished_at | TIMESTAMP | 结束时间 |
| created_at | TIMESTAMP | 创建时间 |

### 4.9 `backtest_order`

用途：订单意图。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| order_id | BIGINT | 主键 |
| run_id | BIGINT | 回测任务主键 |
| security_id | BIGINT | 证券主键 |
| submitted_date | DATE | 发单交易日 |
| intended_exec_date | DATE | 预期执行日期 |
| side | VARCHAR | `buy` / `sell` |
| order_type | VARCHAR | 如 `market_open` |
| quantity | BIGINT | 目标数量 |
| target_weight | DECIMAL(10,6) | 目标权重，可空 |
| status | VARCHAR | 订单状态 |
| reject_reason | VARCHAR | 拒绝或未成交原因 |
| created_at | TIMESTAMP | 创建时间 |

### 4.10 `backtest_fill`

用途：成交记录。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| fill_id | BIGINT | 主键 |
| order_id | BIGINT | 订单主键 |
| run_id | BIGINT | 回测任务主键 |
| security_id | BIGINT | 证券主键 |
| fill_date | DATE | 成交日期 |
| fill_price | DECIMAL(18,4) | 成交价 |
| fill_quantity | BIGINT | 成交数量 |
| gross_amount | DECIMAL(20,2) | 成交金额 |
| commission | DECIMAL(20,2) | 手续费 |
| tax | DECIMAL(20,2) | 税费 |
| slippage_cost | DECIMAL(20,2) | 滑点成本 |
| created_at | TIMESTAMP | 创建时间 |

### 4.11 `position_snapshot`

用途：持仓快照。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| position_snapshot_id | BIGINT | 主键 |
| run_id | BIGINT | 回测任务主键 |
| trade_date | DATE | 交易日 |
| security_id | BIGINT | 证券主键 |
| quantity | BIGINT | 总持仓 |
| sellable_quantity | BIGINT | 可卖数量 |
| avg_cost | DECIMAL(18,4) | 持仓成本 |
| close_price | DECIMAL(18,4) | 当日收盘价 |
| market_value | DECIMAL(20,2) | 当日市值 |
| unrealized_pnl | DECIMAL(20,2) | 浮动盈亏 |
| created_at | TIMESTAMP | 创建时间 |

### 4.12 `equity_snapshot`

用途：每日权益。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| equity_snapshot_id | BIGINT | 主键 |
| run_id | BIGINT | 回测任务主键 |
| trade_date | DATE | 交易日 |
| cash | DECIMAL(20,2) | 现金 |
| market_value | DECIMAL(20,2) | 持仓市值 |
| total_equity | DECIMAL(20,2) | 总权益 |
| daily_pnl | DECIMAL(20,2) | 当日盈亏 |
| cumulative_return | DECIMAL(12,6) | 累计收益率 |
| turnover | DECIMAL(12,6) | 当日换手率，可空 |
| max_drawdown | DECIMAL(12,6) | 截至当日最大回撤，可空 |
| created_at | TIMESTAMP | 创建时间 |

### 4.13 `run_metric`

用途：实验平台汇总指标。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| run_metric_id | BIGINT | 主键 |
| run_id | BIGINT | 回测任务主键 |
| metric_name | VARCHAR | 指标名，如 `score`、`sharpe_ratio` |
| metric_value | DOUBLE | 指标值，可空 |
| created_at | TIMESTAMP | 创建时间 |

### 4.14 `run_artifact`

用途：实验产物索引。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| artifact_id | BIGINT | 主键 |
| run_id | BIGINT | 回测任务主键 |
| artifact_name | VARCHAR | 产物名，如 `summary`、`equity_curve` |
| artifact_type | VARCHAR | 产物类型，如 `json`、`csv` |
| relative_path | TEXT | 相对路径 |
| created_at | TIMESTAMP | 创建时间 |

## 5. 可以后续再加的扩展表

以下表不阻塞第一版落地，但建议后续扩展：

- `corporate_action`：分红、送转、拆合股
- `adjustment_factor`：复权因子
- `trading_status`：停牌、复牌、风险警示、涨跌幅限制口径
- `universe_membership`：指数成分、行业分类、概念分类
- `factor_snapshot`：因子横截面快照
- `signal_snapshot`：每日策略信号
- `rebalance_plan`：调仓计划

## 6. 关系说明

核心关系如下：

```text
security_master 1 --- n daily_bar
security_master 1 --- n event_record
security_master 1 --- n raw_market_request
security_master 1 --- n raw_market_response
security_master 1 --- n backtest_order
security_master 1 --- n backtest_fill
security_master 1 --- n position_snapshot

strategy_definition 1 --- n backtest_run
backtest_run 1 --- n backtest_order
backtest_run 1 --- n backtest_fill
backtest_run 1 --- n position_snapshot
backtest_run 1 --- n equity_snapshot
backtest_run 1 --- n run_metric
backtest_run 1 --- n run_artifact

backtest_order 1 --- n backtest_fill
raw_market_request 1 --- n raw_market_response
```

## 7. 索引与分区建议

### 7.1 DuckDB 逻辑索引建议

DuckDB 不完全等同传统 OLTP 数据库索引设计，第一版重点放在：

- 明确唯一键和业务主键
- 通过排序和分区优化扫描
- 对频繁查询路径建立约束和物化视图预期

建议：

- `daily_bar`：按 `trade_date`、`security_id` 排序，逻辑唯一键为 `(`security_id`, `trade_date`)`
- `event_record`：按 `event_date`、`security_id` 排序
- `backtest_order`：按 `run_id`、`submitted_date` 排序
- `backtest_fill`：按 `run_id`、`fill_date` 排序
- `position_snapshot`：按 `run_id`、`trade_date`、`security_id` 排序
- `equity_snapshot`：按 `run_id`、`trade_date` 排序

### 7.2 Parquet 分区建议

推荐物理存储：

- `raw_market_response`：`data/raw/source_name=.../fetch_date=YYYY-MM-DD/`
- `daily_bar`：`data/standard/daily_bar/year=YYYY/month=MM/`
- `event_record`：`data/standard/event_record/year=YYYY/`
- `position_snapshot`：`data/result/position_snapshot/run_id=.../year=YYYY/`
- `equity_snapshot`：`data/result/equity_snapshot/run_id=.../year=YYYY/`

## 8. DuckDB / Parquet 落地建议

推荐如下：

- 原始层：Parquet 为主，DuckDB 外表查询
- 标准层：Parquet 为主，DuckDB 负责联表查询和研究分析
- 结果层：小规模项目可以直接存 DuckDB，本地研究结果也可导出为 Parquet

建议目录：

```text
data/
  raw/
  standard/
  result/
sql/
  schema_v1.sql
docs/
  data-model-v1.md
```

## 9. 覆盖性复核

本设计已覆盖当前百度数据源样例中的关键输入：

- `marketData` 对应 `daily_bar`
- `reportData` 对应 `event_record`
- 股票代码清单对应 `security_master`
- 日线回测结果对应 `backtest_run`、`backtest_order`、`backtest_fill`、`position_snapshot`、`equity_snapshot`

本设计也已满足第一版场景：

- A 股日线级回测
- 收盘信号、次日成交
- 支持单股和股票池
- 支持多次任务运行
- 支持后续补充复权、停牌和交易状态扩展
