## Why

现有策略主要识别放量启动、回踩和趋势延续，缺少对“当日最高价创上市以来历史新高”这一强势价格行为的专门筛选。参考 `gemini-history-high.py`，新增策略可以把历史新高股票纳入统一的日线选股与回测框架，并避免通达信脚本依赖。

## What Changes

- 新增历史新高选股策略：以信号日最高价不低于此前全部有效交易日最高价为最终硬条件。
- 采用近 250 个交易日新高作为候选预筛，再对候选执行上市以来全历史最高价终审；预筛仅用于性能优化，不改变最终语义。
- 默认过滤非主板、ST/退市股票，并提供上市天数和最短历史长度限制，避免新股和异常数据误入。
- 使用前复权最高价进行历史高点比较，并明确并列历史新高是否视为命中。
- 增加策略 YAML、参数模型、选股排序评分和策略回测默认配置。

## Capabilities

### New Capabilities

- `all-time-high-selection`: 基于日线历史最高价和股票范围过滤的历史新高选股及回测能力。

### Modified Capabilities

无。

## Impact

- 影响 `src/quant/config/models.py`、`src/quant/pattern_selection.py`、策略配置及相关测试。
- 复用 DuckDB `daily_bar` 的前复权最高价和 `security_master` 的板块、名称、上市日期字段。
- 不依赖 TdxQuant 运行时接口；通达信脚本中的自定义板块同步不纳入本策略核心职责。
