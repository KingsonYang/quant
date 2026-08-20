# Change: fix-price-adjustment-bug

## 概述

修复 daily_bar 表混存前复权与不复权价格导致回测收益虚高的缺陷。方案是统一使用 TDXQuant 不复权价格作为数据源，在入库时预计算前复权 OHLC 并持久化，回测时区分指标价格（前复权）和成交价格（不复权）。

## 缺陷分析

### 现状

```
daily_bar
|-- baidu  (97%, 前复权, 有基线漂移)    <-- 问题根源
|-- ths    (不复权)
+-- tdxquant (不复权)
```

- `load_price_cache()` 不区分来源，直接取 `open_price`/`close_price`
- baidu 前复权不同批次基准日不同 -> 同股相邻日价格比扭曲
- baidu(前复权) -> ths/tdxquant(不复权) 切换 -> 虚假跳价

### 宇通客车实例

| 日期 | baidu(前复权) | TDXQuant(不复权) | 前复权(正确) |
|------|-------------|----------------|------------|
| 2023-03-27 Open | 3.77 | 10.27 | 3.77 |
| 2023-03-27 Close | 4.16 | 10.66 | 4.16 |
| 2023-03-28 Open | 4.51 | 11.01 | 4.51 |
| 2023-03-28 Close | 5.23 | 11.73 | 5.23 |

baidu 的前复权价格本身是对的（和通达信一致），但基线漂移导致不同年份不可比，且与 ths/tdxquant 不复权价格混用。

### 前复权算法

前复权 = 线性变换 `QFQ = A * Price + B`，不是比例乘法。

倒序遍历除权除息事件（最新到最旧），初始 `A=1, B=0`：

```python
m = (10 + ShareBonus + Allotment) / 10
c = (Bonus - Allotment * AllotPrice) / 10
A = A / m           # 先更新 A
B = B - A * c        # 用更新后的 A
```

验证：宇通客车 7010 天全部匹配 API 前复权价格，最大误差 0.005。

## 实施计划

### Phase 1: Schema 变更

**文件**: `sql/schema_v1.sql` 或 migration 脚本

1. `daily_bar` 新增字段：
   - `open_qfq DECIMAL(18,4)`
   - `high_qfq DECIMAL(18,4)`
   - `low_qfq DECIMAL(18,4)`
   - `close_qfq DECIMAL(18,4)`
   - `adjust_type VARCHAR DEFAULT 'none'`

2. `event_record` 无需 schema 变更（现有字段足够存储除权除息事件）

### Phase 2: 前复权计算模块

**新文件**: `src/quant/adjust.py`

```python
def compute_qfq_params(
    events: list[DividendEvent],  # 按 ex_date 升序
    target_date: date,
) -> tuple[float, float]:
    """返回 (A, B) 使得 QFQ = A * price + B"""
    # 倒序遍历 target_date 之后的 events
    A, B = 1.0, 0.0
    for event in reversed(events_after_target):
        m = (10 + event.share_bonus + event.allotment) / 10
        c = (event.bonus - event.allotment * event.allot_price) / 10
        A = A / m
        B = B - A * c
    return A, B

def compute_qfq_ohlc(
    prices: dict[date, tuple[float, float, float, float]],  # {date: (o, h, l, c)}
    events: list[DividendEvent],
) -> dict[date, tuple[float, float, float, float]]:
    """计算全量前复权 OHLC"""
    # 对每个日期，找到其对应的 (A, B)，计算 QFQ = A * price + B
```

### Phase 3: Ingest 模块改造

**文件**: `src/quant/ingest/tdxquant.py`

1. `fetch_history()` 已经使用 `dividend_type='none'`，无需修改
2. 新增 `fetch_divid_factors()` -- 调用 `tq.get_divid_factors()` 获取全量除权除息事件
3. `ingest_stock()` 流程改为：
   - 拉取不复权日线
   - 拉取全量除权除息事件 -> 写入 `event_record`
   - 计算前复权 OHLC -> 写入 `daily_bar` 的 `open_qfq`/`high_qfq`/`low_qfq`/`close_qfq`
   - `source_name` 统一为 `tdxquant`，`adjust_type` = `none`（原始价格不复权，qfq 列单独存）

### Phase 4: 回测引擎改造

**文件**: `src/quant/backtest_common.py`

1. `load_price_cache()` -- 保持返回不复权 `open_price`/`close_price`（用于成交模拟）
2. 新增 `load_qfq_price_cache()` -- 返回 `open_qfq`/`close_qfq`（用于指标计算）
3. `load_low_price_cache()` -- 保持返回不复权 `low_price`（用于止损）
4. 新增 `load_low_qfq_cache()` -- 返回 `low_qfq`（备用）

**文件**: `src/quant/strategy_runtime.py`

1. 买入/卖出/持仓市值/止盈止损 -- 继续用 `price_cache`（不复权）
2. 选股信号计算 -- 改用 `qfq_price_cache`（前复权）
3. MA5/MA10/MA20 -- 从 daily_bar 的 `ma5_price` 等改为用 `close_qfq` 计算，或在选股阶段用前复权

### Phase 5: 数据回填

**新命令**: `quant backfill-prices --source tdxquant [--stock CODE] [--start DATE] [--end DATE]`

流程：
1. 遍历所有股票（或指定股票）
2. 对每只股票：
   - DELETE daily_bar WHERE security_id = ? AND source_name = 'baidu'
   - 从 TDXQuant 拉取全量不复权日线 (count=-1)
   - 从 TDXQuant 拉取全量除权除息事件
   - 计算前复权 OHLC
   - INSERT 到 daily_bar（source_name='tdxquant', adjust_type='none'）
   - 除权除息事件写入 event_record
3. 输出回填摘要

### Phase 6: 选股模块适配

**文件**: 选股相关模块（`pattern_selection.py`、`strategy_matrix.py` 等）

选股信号中使用 `close_qfq` 计算均线和形态识别，确保除权除息不产生假信号。

## 文件影响清单

| 文件 | 变更类型 | 说明 |
|------|---------|------|
| `sql/schema_v1.sql` | 修改 | daily_bar 新增 5 个字段 |
| `src/quant/adjust.py` | 新增 | 前复权计算模块 |
| `src/quant/ingest/tdxquant.py` | 修改 | 新增除权除息拉取 + 前复权计算 |
| `src/quant/ingest/storage.py` | 修改 | DailyBarRecord 新增 qfq 字段 |
| `src/quant/ingest/models.py` | 修改 | DailyBarRecord dataclass 新增字段 |
| `src/quant/backtest_common.py` | 修改 | 新增 load_qfq_price_cache / load_low_qfq_cache |
| `src/quant/strategy_runtime.py` | 修改 | 选股用 qfq，成交用不复权 |
| `src/quant/cli.py` | 修改 | 新增 backfill-prices 命令 |

## 风险与约束

1. **回填耗时**：~3000 只股票全量拉取，预计 1-2 小时
2. **TDXQuant 依赖**：需要通达信客户端运行，tqcenter 可用
3. **count=-1 要求**：前复权计算必须全量拉取除权除息事件，不能限定日期范围
4. **向后兼容**：daily_bar 新增字段为 nullable，不影响现有查询；回填前 baidu 数据仍可使用（但收益会虚高）
