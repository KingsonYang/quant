## Why

当前项目已经完成了第一版数据模型和建库脚本，但还缺少把百度 A 股 JSONP 日 K 数据真正接入系统的能力。若没有统一的抓取、解析、标准化和入库流程，现有表结构和 DuckDB 模式无法形成可用的数据底座，因此需要补齐这一条数据落地链路。

## What Changes

- 新增百度 A 股 JSONP 日 K 数据的采集与解析能力。
- 新增将 `marketData` 和 `reportData` 标准化映射到现有数据模型的处理流程。
- 新增将原始响应、标准化日线行情和事件数据写入 DuckDB 的入库能力。
- 新增最小可运行的 CLI/脚本入口，用于对单只股票执行抓取、解析和入库。

## Capabilities

### New Capabilities
- `baidu-jsonp-ingestion`: 定义百度 A 股 JSONP 数据的抓取、解析、标准化和入库行为

### Modified Capabilities

无

## Impact

- 影响 `collector`、`parser`、`storage` 三个核心模块的实现方式
- 复用现有 `backtest-data-model` 中定义的表结构与字段口径
- 为后续回测引擎提供首批可消费的标准化行情与事件数据
