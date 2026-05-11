# trading-calendar-generation Specification

## Purpose
定义基于 `daily_bar` 生成、补充和维护 `trading_calendar` 的行为，为后续回测引擎提供可消费的交易日序列。

## Requirements

### Requirement: 系统必须能够基于日线行情生成交易日历
系统 MUST 能够从 `daily_bar` 表中提取唯一 `trade_date` 集合，按日期升序生成 `trading_calendar` 记录，并将这些日期标记为 `ab` 市场的交易日。生成结果 SHALL 为每条记录写入 `is_trading_day=true`、`prev_trade_date` 和 `next_trade_date`，以支持后续回测按交易日推进。

#### Scenario: 全量模式从日线行情生成完整交易日序列
- **WHEN** 用户执行全量交易日历生成命令，且 `daily_bar` 中已存在多个交易日
- **THEN** 系统 MUST 读取全部唯一 `trade_date`，按升序写入 `trading_calendar`，并为每条记录补齐相邻交易日关系

#### Scenario: 生成结果只包含唯一交易日
- **WHEN** `daily_bar` 中多个证券共享相同的 `trade_date`
- **THEN** 系统 MUST 在 `trading_calendar` 中仅写入一条对应市场的交易日记录，而不是重复写入

### Requirement: 系统必须能够增量补充缺失交易日
系统 MUST 提供增量模式，对比 `daily_bar` 与 `trading_calendar` 中已有的交易日，仅补充当前日历表中缺失的日期。增量写入后，系统 SHALL 重新计算受影响记录的 `prev_trade_date` 与 `next_trade_date`，确保交易日链保持正确。

#### Scenario: 增量模式只写入尚不存在的交易日
- **WHEN** 用户执行增量交易日历命令，且 `daily_bar` 中包含新旧混合的交易日
- **THEN** 系统 MUST 只向 `trading_calendar` 插入原表中缺失的日期，不得重复写入已存在记录

#### Scenario: 增量补充后刷新相邻交易日关系
- **WHEN** 增量模式补充了新的更晚交易日
- **THEN** 系统 MUST 更新原最后一个交易日的 `next_trade_date`，并为新交易日写入正确的 `prev_trade_date`

### Requirement: 系统必须提供交易日历生成命令入口
系统 MUST 提供两个最小可运行的命令行入口，分别用于全量生成交易日历和增量补充交易日。命令执行结束后 SHALL 输出摘要，说明运行模式、扫描到的交易日数量以及实际写入或更新的记录数量。

#### Scenario: 全量命令触发交易日历重建
- **WHEN** 用户执行全量交易日历命令
- **THEN** 系统 MUST 完成从 `daily_bar` 到 `trading_calendar` 的完整生成流程，并输出执行摘要

#### Scenario: 增量命令触发缺失交易日补充
- **WHEN** 用户执行增量交易日历命令
- **THEN** 系统 MUST 完成缺失交易日补充流程，并输出实际新增记录数量的摘要
