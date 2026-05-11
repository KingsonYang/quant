## Why

当前项目已经定义了 `trading_calendar` 表结构，但还没有生成与写入逻辑，导致交易日历仍停留在“有模型、无数据”的状态。既然 `daily_bar` 中的日期天然都是有效交易日，就可以直接复用现有行情数据生成交易日历，为后续回测引擎、T+1 约束和按交易日推进提供可落地的数据基础。

## What Changes

- 新增基于 `daily_bar` 生成 `trading_calendar` 的能力，将已有日线日期批量写入交易日历表。
- 新增基于 `daily_bar` 增量补充 `trading_calendar` 的能力，只写入当前日历表中尚不存在的交易日。
- 新增两个 CLI 入口，分别用于全量重建交易日历和增量补充交易日。
- 约束交易日历生成结果需补齐 `prev_trade_date` 与 `next_trade_date` 关系，满足回测按交易日推进的读取需求。

## Capabilities

### New Capabilities
- `trading-calendar-generation`: 定义从 `daily_bar` 推导、写入并增量维护 `trading_calendar` 的行为与命令入口

### Modified Capabilities

## Impact

- 影响 `src/quant/cli.py` 中命令行入口的扩展方式
- 影响 `src/quant/db/` 或新增服务模块中的交易日历生成与写入逻辑
- 直接使用现有 DuckDB `daily_bar` 与 `trading_calendar` 表，不引入新外部依赖
- 为后续回测引擎提供可消费的交易日序列基础数据
