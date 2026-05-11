# 代码 Review 与本次优化说明

## 本次已完成的优化

1. **修复 `src/quant/backtest.py` 的缺失导入**
   - `_validate_request()` 使用了 `duckdb.DuckDBPyConnection` 类型注解，但文件未导入 `duckdb`。
   - 影响：在较严格的静态检查或某些运行环境中会报 `F821 Undefined name duckdb`。

2. **清理未使用导入**
   - 涉及：`evaluation/metrics.py`、`evolution/runner.py`、`optimization/runner.py`、`validation/runner.py`、`tests/test_selection.py`。
   - 影响：降低噪音，让 `ruff check` 可以通过，后续改代码更容易发现真正问题。

3. **优化模式选股排序逻辑**
   - 修改位置：`src/quant/pattern_selection.py::_sort_candidates()`。
   - 原逻辑用 `reverse=True` 反转整个 tuple key，会导致主排序字段降序时，次级字段也被反转。
   - 新逻辑只反转主排序字段，次级字段保持稳定升序，避免同分股票排序不稳定。
   - 例如：`sort_by: composite_score`、`sort_order: DESC` 时，分数高的在前；同分时仍按股票代码稳定排序。

4. **优化 CLI 错误提示可读性**
   - 修改位置：`src/quant/commands/run.py`。
   - 缺少 `backtest_selection` / `strategy_backtest` 配置块时，错误信息改为先输出核心原因，再附带策略名，避免终端自动换行把关键短语拆开。

5. **为全部策略 YAML 添加中文注释**
   - 范围：`strategies/**/*.yml` 共 25 个策略文件。
   - 覆盖内容：策略元信息、选股类型、规则字段、比较符、阈值、模式参数、回测参数、优化搜索空间。
   - 对规则型策略显式补充了 `selection.type: rules`，与代码默认值一致，不改变策略含义。

## 已做校验

- `ruff check src tests scripts`：通过。
- `python -m compileall -q src tests scripts`：通过。
- 25 个策略 YAML 均通过 `StrategyConfig.model_validate()` 校验。
- 已单独验证的测试：
  - `tests/test_selection.py`：11 passed。
  - `tests/test_strategy_config.py`：测试项逐项可通过；在当前容器中整文件连续运行时偶发超时，单项运行正常。
  - `tests/test_strategy_backtest.py`：6 passed。
  - `tests/test_pattern_strategy.py`：关键 CLI 回测/矩阵流程已手动跑通；单项测试可通过。

## 后续建议优先级

1. **交易成本和滑点**
   - 当前策略回测摘要中 `transaction_costs_included=False`，收益适合相对比较，不适合直接当真实收益预期。

2. **涨跌停、停牌、不可成交处理**
   - 当前回测主要基于“下日开盘买入/收盘卖出”的理想成交模型。
   - A 股策略应补充涨停无法买入、跌停无法卖出、停牌无行情等逻辑。

3. **规则型和模式型回测逻辑仍有重复**
   - `strategy_backtest.py` 和 `strategy_runtime.py` 里有相似的资金回测逻辑。
   - 后续可以抽出统一的回测执行内核，减少修 bug 时漏改一边的风险。

4. **批量模式选股性能**
   - `select_pattern_candidates_in_range_with_connection()` 当前按交易日循环跑 SQL。
   - 数据量大后，建议改成批量日期 SQL 或缓存中间特征。

5. **YAML 注释模板自动化**
   - 这次已把现有 YAML 注释补齐。
   - 后续 `quant optimize` / `quant discover` 生成新策略时，建议在生成器里直接套同一套注释模板，避免新生成文件又变成无注释版本。
