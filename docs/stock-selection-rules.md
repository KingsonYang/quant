# 固定选股条件说明

当前推荐通过 `strategies/*.yml` 维护选股条件，再用：

```powershell
quant run select <strategy> --trade-date 2026-04-14
```

如果你只是想确认规则字段和排序字段，这份文档仍然适用，因为 YAML 中的 `selection.rules` 和旧版 `rules.json` 使用同一套结构。

## YAML 中的规则结构

```yaml
selection:
  logic: AND
  sort_by: last_5d_avg_volume
  sort_order: DESC
  rules:
    - field: amount_ratio_last_5d_avg
      operator: gte
      value: 1.99
    - field: amount_ratio_last_5d_avg
      operator: lte
      value: 2.01
```

字段含义：

- `field`：规则字段名
- `operator`：比较操作符，支持 `eq`、`ne`、`gt`、`gte`、`lt`、`lte`
- `value`：比较值

## 当前支持的固定选股条件

### 基础过滤字段

- `exchange`
- `board`
- `list_status`
- `listed_days`

### 日线字段

- `close_price`
- `change_pct`
- `turnover_ratio`
- `amount`
- `volume`

### 均线 / 均量字段

- `ma5_price`
- `ma10_price`
- `ma20_price`
- `ma5_volume`
- `ma10_volume`
- `ma20_volume`

### 前 5 个交易日窗口字段

以下字段的窗口定义为“当前交易日前 5 个交易日”，不包含当天：

- `prev_5d_avg_volume`
- `prev_5d_sum_volume`
- `volume_ratio_prev_5d_avg`
- `volume_plus_prev_5d_sum`
- `prev_5d_avg_amount`
- `prev_5d_sum_amount`
- `amount_ratio_prev_5d_avg`
- `amount_plus_prev_5d_sum`

### 含当天近 5 日窗口字段

以下字段的窗口定义为“当天 + 前 4 个交易日”，也就是含当天近 5 日：

- `last_5d_avg_volume`
- `last_5d_sum_volume`
- `volume_ratio_last_5d_avg`
- `volume_plus_last_5d_sum`
- `last_5d_avg_amount`
- `last_5d_sum_amount`
- `amount_ratio_last_5d_avg`
- `amount_plus_last_5d_sum`

### 事件字段

- `financial_report_days_since`

## 当前支持的排序字段

- `symbol`
- `name`
- `close_price`
- `change_pct`
- `turnover_ratio`
- `volume`
- `amount`
- `prev_5d_avg_volume`
- `prev_5d_sum_volume`
- `volume_plus_prev_5d_sum`
- `last_5d_avg_volume`
- `last_5d_sum_volume`
- `volume_plus_last_5d_sum`
- `prev_5d_avg_amount`
- `prev_5d_sum_amount`
- `amount_plus_prev_5d_sum`
- `last_5d_avg_amount`
- `last_5d_sum_amount`
- `amount_plus_last_5d_sum`

## 示例策略

示例文件见：

- `strategies/amount_ratio_prev_5d_near_2x.yml`
- `strategies/examples/volume_ratio_prev_5d_gte_2.yml`

运行示例：

```powershell
quant run select amount_ratio_prev_5d_near_2x --trade-date 2026-04-14
quant run backtest-selection amount_ratio_prev_5d_near_2x --start-date 2026-04-01 --end-date 2026-04-14
quant run backtest-strategy amount_ratio_prev_5d_near_2x --start-date 2024-12-30 --end-date 2026-04-20
```

## Legacy 说明

旧版 `quant select-stocks --rules-file rules/rules.json ...` 仍兼容，但推荐迁移到策略 YAML：

- 规则改动集中在一个文件中维护
- 选股、普通回测、策略回测可以共享同一个策略名
- 不需要每次手工拼接长参数命令
