# 涨停启动首次回踩五日线收复策略定义

建议策略 ID：

```text
limit_up_first_pullback_ma5_reclaim_v1
```

建议中文名：

```text
涨停启动首次回踩五日线收复
```

## 1. 策略目标

本策略用于捕捉涨停启动后的第一次回踩修复机会。

核心思想：

```text
先出现 1 到 3 个涨停形成启动结构
→ 随后在限定交易日窗口内完成高位震荡、回调或假破洗盘
→ 整体不能有效破坏首板开盘价这条最终防守线
→ 信号日从五日线下方或附近重新站上五日线
→ 尝试捕捉二次启动前后的买点
```

本策略不追求涨停当天买入，也不追求已经反包过高后的追涨，而是等待启动结构确认后，寻找首次回踩修复点。

---

## 2. 核心术语

### 2.1 首板

连续涨停序列中的第一个涨停日。

字段建议：

```text
first_limit_date
first_limit_open
first_limit_close
first_limit_high
first_limit_low
```

其中 `first_limit_open` 是本策略最重要的最终防守线。

---

### 2.2 最近涨停 T0

信号日前最近一个有效涨停日。

如果是首板结构：

```text
first_limit_date = last_limit_date = T0
```

如果是二连板或三连板结构：

```text
first_limit_date = 连板首板日期
last_limit_date = 连板最后一个涨停日期 = T0
```

字段建议：

```text
last_limit_date
last_limit_open
last_limit_close
last_limit_high
last_limit_low
```

---

### 2.3 连板数量

连续涨停序列的涨停数量。

字段建议：

```text
board_count
```

允许范围：

```text
1 <= board_count <= 3
```

暂不考虑超过三连板的走势。

---

### 2.4 信号日

满足五日线收复条件的选股日。

字段建议：

```text
signal_date
signal_open
signal_close
signal_ma5
```

信号日必须满足：

```text
signal_open <= signal_ma5
signal_close >= signal_ma5
signal_close > signal_open
```

含义：

```text
开盘价在五日线下方或等于五日线
收盘价站上五日线
并且当日收阳
```

这表示信号日是真正从五日线下方或附近穿越上来，而不是已经在五日线上方继续上涨。

---

## 3. 涨停识别规则

一个交易日被识别为有效涨停启动日，建议满足：

```text
change_pct >= limit_up_min_pct
close_price >= high_price * limit_up_close_high_min_ratio
close_price > open_price
volume >= prev5_avg_volume * limit_up_volume_multiple
```

默认参数建议：

```yaml
limit_up_min_pct: 9.5
limit_up_close_high_min_ratio: 0.995
limit_up_volume_multiple: 1.0
```

参数含义：

| 参数 | 含义 |
|---|---|
| `limit_up_min_pct` | 涨停或接近涨停的最低涨幅要求 |
| `limit_up_close_high_min_ratio` | 收盘价接近日内最高价的比例要求 |
| `limit_up_volume_multiple` | 启动日成交量相对前 5 日均量的最低倍数 |

---

## 4. 总周期定义

从最近涨停 T0 之后，到信号日前，允许的总交易日窗口：

```text
pullback_min_trade_days <= T0 后到信号日前的交易日数量 <= pullback_max_trade_days
```

默认参数建议：

```yaml
pullback_min_trade_days: 3
pullback_max_trade_days: 15
```

注意：

```text
高位震荡期也计入 15 个交易日总周期。
```

也就是说，如果 T0 后先高位震荡了 4 天，再回调 5 天，那么总周期按 9 天计算。

---

## 5. 最终防守线

本策略最终防守线统一定义为：

```text
first_limit_open
```

也就是首板开盘价。

无论是首板、二连板还是三连板，最终都以首板开盘价判断启动结构是否被有效破坏。

---

## 6. 分支一：首板直接回踩收复五日线

建议分支名：

```text
single_direct_pullback_reclaim
```

中文名：

```text
首板直接回踩收复五日线
```

### 6.1 形态描述

```text
T0 首板涨停
→ T0 后直接进入回调或弱势整理
→ 回调持续 3 到 15 个交易日
→ 回调期间收盘价不跌破 T0 开盘价
→ 信号日开盘价 <= 五日线，收盘价 >= 五日线，并且收阳
```

### 6.2 条件定义

适用条件：

```text
board_count = 1
enable_direct_pullback = true
```

支撑条件：

```text
min(close_price between T0+1 and signal_date-1) >= first_limit_open
```

信号日条件：

```text
signal_open <= signal_ma5
signal_close >= signal_ma5
signal_close > signal_open
```

---

## 7. 分支二：首板高位平台后回踩收复五日线

建议分支名：

```text
single_platform_then_pullback_reclaim
```

中文名：

```text
首板高位平台后回踩收复五日线
```

### 7.1 形态描述

```text
T0 首板涨停
→ T0 后没有马上回调
→ 先在 T0 收盘价附近或上方震荡
→ 高位震荡期间最高价不超过 T0 close 的指定比例
→ 后续进入回踩或等待 MA5 上移
→ 整个过程仍然放在 3 到 15 个交易日总周期内
→ 回踩期间收盘价不有效跌破 T0 开盘价
→ 信号日穿越并收复五日线
```

### 7.2 高位平台高度限制

默认参数建议：

```yaml
platform_high_limit_pct: 10.0
```

条件：

```text
max(high_price between T0+1 and signal_date-1) <= last_limit_close * (1 + platform_high_limit_pct / 100)
```

对于首板结构：

```text
last_limit_close = T0 close
```

### 7.3 回踩确认模式

建议参数：

```yaml
pullback_confirm_mode: either
```

可选值：

```text
below_last_limit_close
ma5_touch
either
```

含义：

| 模式 | 含义 |
|---|---|
| `below_last_limit_close` | 必须跌破最近涨停收盘价，才算进入回踩 |
| `ma5_touch` | 不要求跌破最近涨停收盘价，只要价格靠近或触碰 MA5 即可 |
| `either` | 跌破最近涨停收盘价，或靠近/触碰 MA5，满足其一即可 |

默认建议：

```text
either
```

这样可以同时覆盖：

```text
涨停后高位震荡再回踩
涨停后横盘不跌、等待 MA5 上移的时间换空间走势
```

---

## 8. 分支三：假破首板开盘价后快速收回

建议分支名：

```text
false_break_first_open_reclaim
```

中文名：

```text
假破首板开盘价后快速收回
```

### 8.1 形态描述

该分支可以叠加在分支一和分支二上。

```text
涨停启动后进入回踩
→ 回踩过程中允许短暂跌破首板开盘价
→ 但跌破幅度不能太大
→ 并且必须在 1 到 2 个交易日内重新收盘站回首板开盘价
→ 随后或同日信号日收复五日线
```

### 8.2 默认参数

```yaml
enable_false_break: true
false_break_tolerance_pct: 5.0
false_break_reclaim_days: 2
```

### 8.3 假破幅度定义

使用最低价判断假破幅度：

```text
min(low_price after T0 before signal) >= first_limit_open * (1 - false_break_tolerance_pct / 100)
```

默认即：

```text
最低价不能低于首板开盘价的 95%
```

### 8.4 快速收回定义

一旦出现：

```text
low_price < first_limit_open
```

则要求在最多 `false_break_reclaim_days` 个交易日内出现：

```text
close_price >= first_limit_open
```

默认即：

```text
跌破后 2 个交易日内收盘重新站回首板开盘价
```

当天盘中假破、当天收回也算满足条件。

---

## 9. 分支四：二连板 / 三连板后回踩收复五日线

建议分支名：

```text
multi_limit_pullback_reclaim
```

中文名：

```text
二连板三连板后回踩收复五日线
```

### 9.1 形态描述

```text
连续 2 到 3 个涨停形成启动结构
→ 最近一个涨停作为 T0
→ T0 后进入震荡或回调
→ 最终防守线看连板首板 open
→ 信号日重新收复五日线
```

### 9.2 连板数量限制

默认参数建议：

```yaml
min_board_count: 2
max_board_count: 3
```

总策略中可以设置：

```yaml
min_board_count: 1
max_board_count: 3
```

首板策略中可以设置：

```yaml
min_board_count: 1
max_board_count: 1
```

### 9.3 双锚点判断

连板分支需要同时参考两个锚点。

#### 锚点 A：首板 open

用于判断整个启动结构是否被破坏。

标准防守：

```text
min(close_price after T0 before signal) >= first_limit_open
```

假破防守：

```text
min(low_price after T0 before signal) >= first_limit_open * (1 - false_break_tolerance_pct / 100)
并且 1 到 2 个交易日内 close_price >= first_limit_open
```

#### 锚点 B：最近涨停 T0 close

用于判断信号日是否已经过热。

建议默认控制：

```yaml
signal_max_above_last_limit_close_pct: 0.0
```

即：

```text
signal_close <= last_limit_close
```

含义：

```text
不买已经重新涨回甚至超过最近涨停收盘价太多的票。
```

后续优化时可以尝试：

```yaml
signal_max_above_last_limit_close_pct: [0.0, 3.0, 5.0, 10.0]
```

---

## 10. 时间换空间形态归类

时间换空间不建议单独作为一个策略分支，而是归入：

```text
single_platform_then_pullback_reclaim
```

其特征是：

```text
T0 后没有明显下跌
价格在 T0 close 附近横盘
MA5 逐渐上移
某天开盘在 MA5 下方或附近
收盘重新站上 MA5
```

该形态可以通过：

```yaml
pullback_confirm_mode: ma5_touch
```

或：

```yaml
pullback_confirm_mode: either
```

纳入。

---

## 11. 第二天冲高回落形态归类

第二天冲高回落不建议单独作为策略分支，而是归入：

```text
single_direct_pullback_reclaim
```

其特征是：

```text
T0 后第二天可能先冲高
但最终收弱
随后进入回调
```

可选判断：

```text
T1 close < T0 close
或
T1 close < T1 open
```

但该条件建议作为辅助识别，不建议过早做成硬过滤。

---

## 12. 首板 open 与 MA5 共振

首板开盘价支撑与 MA5 支撑接近时，通常代表更强的技术共振。

例如：

```text
first_limit_open 与 signal_ma5 距离较近
```

建议作为排序加分项，而不是硬过滤条件。

可选打分项：

```text
support_ma5_confluence_score
```

含义：

```text
first_limit_open 与 signal_ma5 越接近，分数越高
```

---

## 13. 推荐策略拆分方式

建议采用：

```text
一个核心 pattern_id
多个 YAML 策略预设
```

核心模式：

```text
limit_up_first_pullback_ma5_reclaim_v1
```

推荐 YAML 策略：

```text
limit_up_first_pullback_ma5_reclaim_all.yml
limit_up_first_pullback_ma5_reclaim_single_standard.yml
limit_up_first_pullback_ma5_reclaim_single_false_break.yml
limit_up_first_pullback_ma5_reclaim_multi_board.yml
```

---

## 14. 策略一：全分支总策略

建议策略名：

```text
limit_up_first_pullback_ma5_reclaim_all
```

包含分支：

```text
single_direct_pullback_reclaim
single_platform_then_pullback_reclaim
false_break_first_open_reclaim
multi_limit_pullback_reclaim
```

用途：

```text
实盘候选池
观察整体策略族效果
```

优点：

```text
覆盖完整
样本较多
更适合发现机会
```

缺点：

```text
不同形态混在一起，优化结果可能互相污染
```

---

## 15. 策略二：首板标准版

建议策略名：

```text
limit_up_first_pullback_ma5_reclaim_single_standard
```

包含分支：

```text
single_direct_pullback_reclaim
single_platform_then_pullback_reclaim
```

不包含：

```text
false_break_first_open_reclaim
multi_limit_pullback_reclaim
```

用途：

```text
验证最干净的首板回踩形态是否有稳定优势
```

这是建议最先单独回测的版本。

---

## 16. 策略三：首板假破版

建议策略名：

```text
limit_up_first_pullback_ma5_reclaim_single_false_break
```

包含分支：

```text
false_break_first_open_reclaim
```

适用对象：

```text
board_count = 1
```

用途：

```text
验证洗盘打穿首板开盘价后快速修复的形态是否有独立优势
```

该分支风险和弹性通常都更大，建议单独统计。

---

## 17. 策略四：连板回踩版

建议策略名：

```text
limit_up_first_pullback_ma5_reclaim_multi_board
```

包含分支：

```text
multi_limit_pullback_reclaim
```

适用对象：

```text
2 <= board_count <= 3
```

用途：

```text
验证二连板 / 三连板高潮后的第一次回踩修复是否有独立优势
```

该分支和首板回踩的资金行为差异较大，建议单独回测和优化。

---

## 18. 建议输出字段

为了后续回测、归因、优化和样本复盘，建议模式选股结果至少输出：

```text
symbol
name
selection_date
branch_type
board_count
first_limit_date
first_limit_open
first_limit_close
last_limit_date
last_limit_open
last_limit_close
signal_open
signal_close
signal_ma5
min_close_after_last_limit
min_low_after_last_limit
max_high_after_last_limit
false_break_occurred
false_break_reclaim_days_actual
composite_score
```

其中：

| 字段 | 含义 |
|---|---|
| `branch_type` | 命中的具体分支 |
| `board_count` | 连板数量 |
| `first_limit_date` | 首板日期 |
| `last_limit_date` | 最近涨停日期，也就是 T0 |
| `false_break_occurred` | 是否发生假破 |
| `false_break_reclaim_days_actual` | 实际几天收回 |
| `composite_score` | 综合排序分 |

---

## 19. 建议可优化参数

建议纳入优化的参数：

```yaml
pullback_min_trade_days: [2, 3, 4]
pullback_max_trade_days: [10, 15, 20]
platform_high_limit_pct: [5.0, 8.0, 10.0, 12.0]
false_break_tolerance_pct: [3.0, 5.0, 7.0]
false_break_reclaim_days: [1, 2, 3]
signal_min_change_pct: [0.0, 0.5, 1.0]
signal_max_above_last_limit_close_pct: [0.0, 3.0, 5.0, 10.0]
limit_up_volume_multiple: [0.8, 1.0, 1.2, 1.5]
```

建议先不要优化的结构性参数：

```yaml
signal_open_below_ma5: true
signal_close_above_ma5: true
signal_require_bullish_candle: true
support_anchor: first_limit_open
```

原因：

```text
这些属于策略定义本身，不建议在早期优化中被搜索器改掉。
```

---

## 20. 建议默认参数

```yaml
selection:
  type: pattern_sql
  pattern_id: limit_up_first_pullback_ma5_reclaim_v1
  sort_by: composite_score
  sort_order: DESC
  params:
    min_board_count: 1
    max_board_count: 3

    limit_up_min_pct: 9.5
    limit_up_close_high_min_ratio: 0.995
    limit_up_volume_multiple: 1.0

    pullback_min_trade_days: 3
    pullback_max_trade_days: 15

    enable_direct_pullback: true
    enable_high_platform: true
    platform_high_limit_pct: 10.0
    pullback_confirm_mode: either

    support_anchor: first_limit_open
    standard_support_mode: close

    enable_false_break: true
    false_break_tolerance_pct: 5.0
    false_break_reclaim_days: 2

    signal_open_below_ma5: true
    signal_close_above_ma5: true
    signal_require_bullish_candle: true
    signal_min_change_pct: 0.0
    signal_max_above_last_limit_close_pct: 0.0
```

---

## 21. 策略实现优先级建议

建议实现顺序：

```text
1. 首板标准版
2. 首板假破版
3. 连板回踩版
4. 全分支总策略
5. 输出 branch_type 和关键锚点字段
6. 再做优化和进化
```

原因：

```text
先验证最干净、最容易解释的首板标准版。
再验证假破是否带来增益。
最后单独验证连板分支。
避免一开始把所有形态混在一起，导致优化和回测归因困难。
```

---

## 22. 与旧策略的主要差异

旧策略：

```text
limit_up_pullback_ma5_reclaim_v1
```

主要使用：

```text
T0 low_price 作为支撑参考
信号日 close >= ma5 即可
允许较宽泛的深回撤
没有明确识别高位平台、假破快速收回、连板双锚点
```

新策略：

```text
limit_up_first_pullback_ma5_reclaim_v1
```

核心变化：

```text
1. 支撑锚点改为首板 open
2. 标准分支使用 close >= first_limit_open
3. 假破分支使用 low 判断破位幅度，close 判断快速收回
4. 信号日要求 open <= ma5 <= close 且 close > open
5. 支持首板、高位平台、假破、二连板/三连板四类分支
6. 输出 branch_type，方便回测归因
```

---

## 23. 最终策略定义摘要

```text
涨停启动首次回踩五日线收复策略：
在最近 1 到 3 个涨停形成的启动结构之后，等待 3 到 15 个交易日内的第一次回踩或震荡修复。只要整体没有有效破坏首板开盘价，或者虽短暂假破但能在 1 到 2 个交易日内快速收回，则继续保留。信号日必须开盘在五日线下方或等于五日线，收盘站上五日线，并且收阳。该策略用于捕捉涨停启动结构未坏、首次回踩完成后的二次启动机会。
```
