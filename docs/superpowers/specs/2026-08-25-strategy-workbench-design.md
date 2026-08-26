# 策略工作台第一版设计

日期：2026-08-25  
范围：Web 工作台 v1（数据维护 + 选股展示 + 策略回测续跑）

## 1. 目标

给 quant 做一个本地 Web 工作台，覆盖三件日常事：

1. 用 TDXQuant 增量补行情，并同步交易日历
2. 跑 `quant run select`，看某日候选股
3. 跑 `quant run backtest-strategy`，看净值/回撤/交割明细；开始日不变时，结束日往后推只追加计算，不整段重跑

工作台是 CLI 的可视化层，不替代 YAML 策略文件，不在网页里改策略参数。

## 2. 信息架构

顶栏三个入口，一次只做一件事（全局同一时刻只允许一个后台任务）：

| 页面 | 对应 CLI | 用户来干什么 |
|------|----------|--------------|
| 数据 | `quant ingest-tdxquant`、`quant build-trading-calendar-incremental` | 先把行情和日历补齐 |
| 选股 | `quant run select <strategy> --trade-date` | 看某日谁进池 |
| 策略回测 | `quant run backtest-strategy` | 看资金曲线、交割、往后追加 |

不做：普通回测页、优化/验证/进化、YAML 编辑、实盘下单、百度/同花顺入库、`backfill-prices`、`compact-db`。

## 3. 数据页

### 状态（只读）

- `daily_bar` 最新交易日、覆盖股票数
- `trading_calendar` 最新交易日
- 两者是否对齐：日历落后行情时给出「请同步日历」提示
- TDX 连接是否可用（不可用时禁用增量按钮，提示先开通行情终端）

### 操作

- **增量更新行情**：调用现有 `ingest_tdxquant`，默认最近 5 个交易日、主板。页面可改天数，不可改数据源。
- **同步交易日历**：调用现有 `build_trading_calendar_incremental`。
- 任务日志区：显示进度和结束摘要（写入行数、跳过行数、错误）。

约束（沿用现有入库逻辑，页面不改语义）：

- TDXQuant 只补充 baidu/ths 尚未覆盖的日期，不覆盖已有来源
- 依赖本机通达信 / tqcenter
- 增量完成后若日历落后，页面明确提示下一步点「同步交易日历」

## 4. 选股页

### 操作

- 策略下拉：扫描 `strategies/*.yml`
- 交易日：默认日历中最新交易日
- 按钮：选股

### 展示

对应 `StrategySelectionView` / `StrategySelectionCandidate`，表格字段：

- 代码、名称
- `d0_date`（有则显示）
- `composite_score`、`pool_score_v3`（有则显示）
- 形态附加列按需显示：`branch_type`、`board_count`、首末涨停日、是否假突破
- 命中数量

空结果要明确「当日 0 只」，不要空白页。选股失败（缺数据、非交易日、策略没有 selection）显示错误，不写实验记录。

选股结果本次不强制落库；需要回看时重新跑即可。第一版不把选股结果接进回测追加。

## 5. 策略回测页

### 顶栏

- 策略下拉
- 开始日 / 结束日
- 主按钮：
  - 没有可续跑的 run → 「运行」（整段 `start → end`）
  - 已有「同一策略 + 同一开始日 + 同一参数快照」且新结束日 > 已算到的日期 → 「追加」
- 旁注：`run_id`、已算到的日期、任务状态

参数只读展示 YAML 快照（资金、仓位、止盈止损、`selection_top_n` 等）。要改参数就改 YAML 后新开一条 run。

### 主图与指标

运行中按交易日推送点，曲线向右增长：

- 净值：`equity_snapshot.total_equity`
- 回撤：由净值序列计算或使用已有 `max_drawdown`

指标条：总收益、年化、最大回撤、成交笔数、当前现金、当前市值。

### 页签

- **交割明细**：买卖日期、价格、股数、金额、持有天数、收益、退出原因
- **持仓**：期末未平仓
- **参数**：本次 `parameter_snapshot`
- **分年**：已有 `yearly_metrics.json`

### 保存与追加

沿用现有 `ExperimentStore` 表：`backtest_run`、`run_metric`、`equity_snapshot`、`position_snapshot`、artifacts（`trades.csv` 等）。

追加规则：

- 续跑键：`strategy_key + start_date + parameter_snapshot` 哈希
- 第一次：整段计算并写入快照
- 追加：读取该 run 最后交易日的现金和持仓，只计算 `(last_end, new_end]`，插入新的成交和净值/持仓快照，更新 `end_date` 与指标
- 不可追加：参数变了、开始日变了、新结束日不大于已算日期、已有任务在跑
- 追加失败不得破坏已有区间数据（先写新区间，成功后再更新 `end_date`；失败则该 run 仍停在旧结束日）

引擎现状是整段重跑，第一版必须改 `run_loaded_strategy_backtest`（或并列的 resume 入口），Web 不得自己复制一套成交逻辑。

## 6. 技术结构

- 新包：`src/quant/web/`（工作台，不是已删除的旧回测表单）
- 框架：FastAPI + HTML 模板 + 少量 JS（曲线）。不引入前端工程（无 React/Vite）
- 启动：`quant web`（本地 `127.0.0.1`，可配端口）
- 任务：进程内单槽队列。回测/入库用现有 `progress_callback`，经 SSE 或短轮询推到页面
- 回测读写：只走 `strategy_runtime` + `ExperimentStore`；选股走 `run_strategy_selection`；数据走现有 ingest/calendar 函数

## 7. 错误处理

- 通达信未开：数据页禁用增量，说明原因
- 非交易日：选股/回测拒绝并提示最近交易日
- 策略缺 `strategy_backtest`：回测页拒绝
- 任务冲突：第二下点击返回「已有任务在跑」
- 回测中途失败：`run_status=failed`，已成功写入的追加区间按第 5 节回滚/不提交结束日

## 8. 验证标准

- 数据页能完成一次 TDX 增量，日历能追上 `daily_bar` 最大日期
- 选股页对现有 YAML 跑一天，表格字段与 CLI 一致
- 策略回测：新 run 能出净值、回撤、交割；把结束日推后能追加且成交笔数只增不重
- 改 YAML 参数后不能点追加，只能新 run
- 同时只跑一个任务
- CLI 原有 `quant run` / `quant ingest-tdxquant` / 日历命令行为不变

## 9. 非目标

- `backtest-selection` 页面
- 多 run 并排对比
- 在网页编辑策略
- 实盘选股 SOP / v3 live
- 修复复权混用（独立 change，不绑在本工作台）
