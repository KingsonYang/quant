# 策略 YAML 配置说明

项目当前推荐把选股、普通回测、策略回测、参数优化统一写进 `strategies/*.yml`，再通过策略名执行。

## 目录约定

- `strategies/`: 正式使用的策略文件
- `strategies/base/`: 阶段性研究基准策略，承接更宽的优化搜索空间
- `strategies/generated/`: `quant optimize` 生成的派生策略
- `strategies/examples/`: 示例策略，不参与正式运行

文件名必须和 `strategy.name` 一致。比如：

- 文件：`strategies/shandian_1hao_erci_baoliang_qidong.yml`
- `strategy.name`：`shandian_1hao_erci_baoliang_qidong`

## 顶层结构

```yaml
version: 1
strategy:
  name: shandian_1hao_erci_baoliang_qidong
  display_name: 闪电一号之二次爆量启动
  description: D0 爆量阳线、平台不破低点、D1 二次爆量启动
selection:
  type: pattern_sql
  pattern_id: second_volume_launch_v1
  sort_by: symbol
  sort_order: ASC
  params:
    lookback_calendar_days: 120
    platform_max_trade_days: 30
    k0_volume_multiple: 2.0
    k1_volume_multiple: 1.5
    d0_change_min_pct: 5.0
    d1_change_min_pct: 5.0
    d1_prev2_max_change_pct: 10.0
  filter_rules:
    - field: change_pct
      operator: gte
      value: 5.74
    - field: change_pct
      operator: lte
      value: 6.61
backtest_selection:
  holding_period: 1
  selection_top_n: 1
strategy_backtest:
  holding_period: 3
  max_positions: 1
  max_daily_buys: 1
  initial_capital: 100000
  position_size_pct: 100
  take_profit_pct: 20
  stop_loss_pct: -5
  selection_top_n: 1
optimization:
  enabled: true
  search_space:
    selection.params.k0_volume_multiple: [1.5, 2.0, 2.5]
    selection.params.k1_volume_multiple: [1.2, 1.5, 1.8]
    strategy_backtest.holding_period: [2, 3, 5]
```

## 字段说明

### `strategy`

- `name`: CLI 使用的稳定策略 id，必须和文件名一致
- `display_name`: 可选，用于中文展示
- `description`: 可选，策略描述

### `selection`

支持两类策略定义。

#### 1. 规则型策略

如果没有显式写 `type`，系统会自动按 `rules` 处理。

```yaml
selection:
  rules:
    - field: close_price
      operator: gt
      value: 0
  logic: AND
  sort_by: volume
  sort_order: DESC
```

字段说明：

- `type`: 可省略；显式写法为 `rules`
- `rules`: 规则列表，结构沿用原来的规则模型
- `logic`: 当前只支持 `AND`
- `sort_by`: 排序字段
- `sort_order`: `ASC` / `DESC`

#### 2. 模式型策略

当前内置模式：

- `pattern_id: second_volume_launch_v1`

它对应 “D0 爆量阳线 -> 平台整理不破低点 -> D1 二次爆量启动”。

```yaml
selection:
  type: pattern_sql
  pattern_id: second_volume_launch_v1
  sort_by: symbol
  sort_order: ASC
  params:
    lookback_calendar_days: 120
    platform_max_trade_days: 30
    k0_volume_multiple: 2.0
    k1_volume_multiple: 1.5
    d0_change_min_pct: 5.0
    d1_change_min_pct: 5.0
    d1_prev2_max_change_pct: 10.0
```

参数说明：

- `lookback_calendar_days`: 回看自然日窗口
- `platform_max_trade_days`: D0 到 D1 之间允许的平台最大交易日数
- `k0_volume_multiple`: D0 成交量相对前序均量倍数
- `k1_volume_multiple`: D1 成交量相对前一日成交量倍数
- `d0_change_min_pct`: D0 最小涨幅
- `d1_change_min_pct`: D1 最小涨幅
- `d1_prev2_max_change_pct`: D1 前两日涨幅和上限

### `backtest_selection`

`pattern_sql` 策略可以配置可选的 `selection.filter_rules`，表示模式命中后的二级过滤。它只支持当前候选日可计算的数值字段和 `eq/ne/gt/gte/lt/lte` 操作符；运行时顺序是“模式命中 -> filter_rules 过滤 -> selection_top_n”。`quant discover filters` 生成的 pattern 候选策略会把发现出的过滤区间追加到这里。

可选。运行 `quant run backtest-selection <strategy>` 时需要：

- `holding_period`
- `selection_top_n`

### `strategy_backtest`

可选。运行 `quant run backtest-strategy <strategy>` 或持有期矩阵回测时需要：

- `holding_period`
- `max_positions`
- `max_daily_buys`
- `initial_capital`
- `position_size_pct`
- `take_profit_pct`
- `stop_loss_pct`
- `selection_top_n`

### `optimization`

可选。启用后，`quant optimize <strategy>` 可以基于该搜索空间自动优化参数。

```yaml
optimization:
  enabled: true
  search_space:
    selection.rules.0.value: [1.8, 1.9, 1.99]
    selection.sort_by: [last_5d_avg_volume, amount]
    selection.sort_order: [ASC, DESC]
    strategy_backtest.holding_period: [2, 3, 5]
    strategy_backtest.take_profit_pct: [10, 20, 30]
    strategy_backtest.stop_loss_pct: [-10, -5]
```

当前第一版支持的参数路径白名单：

- `backtest_selection.holding_period`
- `backtest_selection.selection_top_n`
- `selection.rules.<index>.value`
- `selection.sort_by`
- `selection.sort_order`
- `strategy_backtest.holding_period`
- `strategy_backtest.max_positions`
- `strategy_backtest.max_daily_buys`
- `strategy_backtest.position_size_pct`
- `strategy_backtest.take_profit_pct`
- `strategy_backtest.stop_loss_pct`
- `strategy_backtest.selection_top_n`
- `selection.params.k0_volume_multiple`
- `selection.params.k1_volume_multiple`
- `selection.params.platform_max_trade_days`
- `selection.params.d0_change_min_pct`
- `selection.params.d1_change_min_pct`
- `selection.params.d1_prev2_max_change_pct`

说明：

- `selection.rules.<index>.value` 只支持 `rules` 型策略，`<index>` 从 0 开始，对应 YAML 中第几个规则
- `selection.sort_by` 会按策略类型校验：规则型使用规则选股排序字段，模式型使用模式选股排序字段
- `selection.sort_order` 支持 `ASC` / `DESC`

## 运行命令

列出策略：

```powershell
quant strategy list
```

查看策略：

```powershell
quant strategy show shandian_1hao_erci_baoliang_qidong
```

校验策略：

```powershell
quant strategy validate
quant strategy validate shandian_1hao_erci_baoliang_qidong
```

按策略选股：

```powershell
quant run select shandian_1hao_erci_baoliang_qidong --trade-date 2026-04-21
```

普通回测：

```powershell
quant run backtest-selection shandian_1hao_erci_baoliang_qidong --start-date 2025-12-31 --end-date 2026-04-21
```

策略回测：

```powershell
quant run backtest-strategy shandian_1hao_erci_baoliang_qidong --start-date 2025-12-31 --end-date 2026-04-21
```

持有期矩阵回测：

```powershell
quant run matrix strategy-holding-periods shandian_1hao_erci_baoliang_qidong --start-date 2025-12-31 --end-date 2026-04-21 --holding-periods 1,2,3,4,5
```

参数优化：

```powershell
quant optimize amount_ratio_prev_5d_near_2x --start-date 2026-04-01 --end-date 2026-04-14
quant optimize shandian_1hao_erci_baoliang_qidong --target selection_backtest --method random --max-trials 20 --seed 7 --start-date 2025-12-31 --end-date 2026-04-21
python -m quant optimize amount_ratio_prev_5d_near_2x_phase2 --strategies-dir strategies/base --method random --max-trials 20 --seed 7 --target strategy_backtest --start-date 2026-04-01 --end-date 2026-05-06 --top-n 3
python -m quant optimize shandian_1hao_erci_baoliang_qidong_phase2 --strategies-dir strategies/base --method random --max-trials 20 --seed 7 --target selection_backtest --start-date 2025-12-31 --end-date 2026-05-06 --top-n 3
```

稳健性验证：

```powershell
quant validate amount_ratio_prev_5d_near_2x_phase2__strategy_backtest__rank_1 --strategies-dir strategies/generated --target strategy_backtest --start-date 2026-04-01 --end-date 2026-05-06
quant validate shandian_1hao_erci_baoliang_qidong_phase2__selection_backtest__rank_1 --strategies-dir strategies/generated --target selection_backtest --mode all --start-date 2025-12-31 --end-date 2026-05-06
```

策略进化：

```powershell
quant evolve amount_ratio_prev_5d_near_2x_phase2__strategy_backtest__rank_1 --strategies-dir strategies/generated --target strategy_backtest --max-mutations 20 --top-n 5 --seed 7 --start-date 2026-04-01 --end-date 2026-05-06
quant evolve shandian_1hao_erci_baoliang_qidong_phase2__selection_backtest__rank_1 --strategies-dir strategies/generated --target selection_backtest --max-mutations 20 --top-n 5 --seed 7 --start-date 2025-12-31 --end-date 2026-05-06
```

优化结果会写到：

主动发现过滤规则：

```powershell
quant discover filters amount_ratio_prev_5d_near_2x__selection_backtest__rank_1 --strategies-dir strategies/generated --target selection_backtest --start-date 2022-12-29 --end-date 2026-05-06 --top-rules 5 --bins 5 --min-samples 50 --seed 7
quant discover filters shandian_1hao_erci_baoliang_qidong --target selection_backtest --start-date 2022-12-29 --end-date 2026-05-06 --top-rules 5 --bins 5 --min-samples 30 --seed 7
quant validate shandian_1hao_erci_baoliang_qidong__discover_filter_rank_1 --strategies-dir strategies/generated --target selection_backtest --mode split --start-date 2022-12-29 --end-date 2026-05-06 --seed 7
quant validate amount_ratio_prev_5d_near_2x__selection_backtest__rank_1__discover_filter_rank_1 --strategies-dir strategies/generated --target selection_backtest --mode all --start-date 2022-12-29 --end-date 2026-05-06 --walk-folds 5 --max-perturbations 20 --seed 7
```

- `results/optimization/...`
- `strategies/generated/*.yml`

生成出来的派生策略仍然可以继续验证和回测：

```powershell
quant strategy validate amount_ratio_prev_5d_near_2x__strategy_backtest__rank_1 --strategies-dir strategies/generated
quant run backtest-strategy amount_ratio_prev_5d_near_2x__strategy_backtest__rank_1 --strategies-dir strategies/generated --start-date 2026-04-01 --end-date 2026-04-14
```

## 允许 CLI 覆盖的运行时参数

YAML 方案下，CLI 只允许覆盖运行时参数：

- `trade_date`
- `start_date`
- `end_date`
- `db_path`
- `show_progress`

以下参数必须维护在 YAML 里，不允许通过 `quant run ...` 临时覆盖：

- 规则或模式定义
- 排序规则
- `selection_top_n`
- 默认持有期
- 资金和仓位参数
- 止盈止损参数

## Legacy 入口

以下命令继续兼容，但不再作为主推荐流程：

- `quant select-stocks`
- `quant backtest-selection`
- `quant backtest-strategy`

研究脚本 `scripts/research/backtest_pattern_selection_hps.py` 也继续保留，但只是兼容包装层。推荐迁移到 `quant run matrix strategy-holding-periods ...`。
