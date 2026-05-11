# 策略自优化与自进化路线图

这份文档用于固化项目下一阶段的发展方向，避免后续对话里丢失“项目要往哪里走”的共识。

后续如果要继续讨论，可以直接引用：

- `docs/strategy-evolution-roadmap.md`
- 2026-05-07 补充状态：`quant discover filters <strategy>` 已支持 `rules` 与 `pattern_sql`；`pattern_sql` 发现策略保留原模式定义，并把二级过滤写入 `selection.filter_rules`；已实跑 `shandian_1hao_erci_baoliang_qidong` discovery，生成 `strategies/generated/shandian_1hao_erci_baoliang_qidong__discover_filter_rank_1..5.yml`。
- “策略自优化/自进化路线图”

## 总目标

把项目从“手工定义策略并回测”，升级为“系统自动生成候选策略、自动回测、自动评分、自动筛选、自动迭代优化”的本地研究引擎。

最终形成一个闭环：

1. 生成候选策略
2. 自动回测
3. 自动评估
4. 自动筛选
5. 自动变异或发现下一代策略
6. 周期性重复

注意：核心目标不是盲目提高收益率，而是持续提升策略的风险收益比、稳定性和可执行性。

## 核心原则

### 1. 不以“最高收益率”作为唯一目标

必须同时考虑：

- 收益率
- 最大回撤
- 稳定性
- 样本数量
- 不同市场阶段的一致性
- 真实交易可执行性

### 2. 防止过拟合优先于盲目扩展

策略自动进化最容易产生“历史很好看、未来不能用”的假策略，因此后续所有优化和发现能力都必须以：

- 训练/验证/测试集分离
- Walk-forward 验证
- 参数扰动稳定性测试

为前提。

### 3. LLM 只能提出假设，不能决定策略有效

无论是人工写出的策略，还是系统自动发现的策略，最终都必须经过统一回测和稳健性验证。

## 已有基础

当前项目已经具备以下基础能力：

- 数据入库与增量更新
- 交易日历构建
- YAML 策略配置
- 规则型策略选股与回测
- 模式型策略选股与回测
- 持有期矩阵回测
- `quant strategy ...`
- `quant run ...`
- `quant summarize ...`

这意味着项目已经具备“策略执行”和“实验运行”的基础，只差“自动优化、自动验证、自动发现、自动迭代”这几层。

## 总体技术路线

建议按以下顺序推进：

1. 把项目升级为实验平台
2. 做自动参数优化
3. 做稳健性验证
4. 做策略进化
5. 做主动策略发现
6. 做长期自动调度

## 第一阶段：实验平台化

目标：让每次回测都变成可持久化、可比较、可复盘的实验记录。

### 需要补齐的能力

- 实验结果持久化
- 统一指标体系
- 统一综合评分
- 实验列表与实验对比

### 每次实验至少要记录

- 策略 YAML 快照
- 回测时间区间
- 参数组合
- 回测指标
- 结果文件路径
- 运行时间
- 数据版本

### 建议新增模块

- `src/quant/experiments/models.py`
- `src/quant/experiments/store.py`
- `src/quant/evaluation/metrics.py`
- `src/quant/evaluation/scoring.py`

### 建议新增命令

- `quant experiment list`
- `quant experiment show <run_id>`
- `quant experiment compare <run_id_1> <run_id_2>`

当前状态：

- 第一阶段已经完成首版落地
- `quant run backtest-selection` / `quant run backtest-strategy` 默认自动保存实验
- `quant experiment list/show/compare` 已可用
- 指标、评分、artifact、结果层落盘已经打通
- 后续默认从第二阶段 `quant optimize <strategy>` 开始推进

## 第二阶段：自动优化已有策略

目标：先让系统学会优化你已经给出的策略，而不是一开始就自由发明策略。

### 可优化对象

规则型策略：

- 阈值
- 排序字段
- `selection_top_n`
- `holding_period`
- `take_profit_pct`
- `stop_loss_pct`

模式型策略：

- `k0_volume_multiple`
- `k1_volume_multiple`
- `platform_max_trade_days`
- `d0_change_min_pct`
- `d1_change_min_pct`
- `d1_prev2_max_change_pct`
- `holding_period`

### 建议策略配置扩展

后续可以在 YAML 中增加：

```yaml
optimization:
  enabled: true
  search_space:
    selection.params.k0_volume_multiple: [1.5, 2.0, 2.5, 3.0]
    selection.params.k1_volume_multiple: [1.2, 1.5, 1.8, 2.0]
    strategy_backtest.holding_period: [1, 2, 3, 5, 8]
```

### 建议新增模块

- `src/quant/optimization/search_space.py`
- `src/quant/optimization/grid_search.py`
- `src/quant/optimization/random_search.py`
- `src/quant/optimization/runner.py`

### 建议新增命令

- `quant optimize <strategy>`
- `quant optimize <strategy> --method grid`
- `quant optimize <strategy> --method random`
- `quant optimize <strategy> --method bayes`

第一版优先做：

- `grid`
- `random`

当前状态（2026-05-06）：

- 第二阶段已经完成首版增强落地
- `quant optimize <strategy>` 支持 `grid` / `random`
- 已支持优化规则型策略的 `selection.rules.<index>.value`
- 已支持优化 `selection.sort_by` / `selection.sort_order`
- 已新增 `strategies/base/` 二阶段基准策略
- 已用 20 次随机搜索跑通规则型与模式型基准策略
- 优化结果已落盘到 `results/optimization/...`
- Top N 派生策略已生成到 `strategies/generated/*.yml`
- 后续默认从第三阶段 `quant validate <strategy>` 开始推进

## 第三阶段：稳健性验证

目标：不只是找历史最优，而是找未来更可能继续有效的策略。

### 必须加入的验证方式

- 训练集 / 验证集 / 测试集分离
- Walk-forward 验证
- 参数扰动稳定性测试

### 建议新增模块

- `src/quant/validation/splits.py`
- `src/quant/validation/walk_forward.py`
- `src/quant/validation/robustness.py`

### 建议新增命令

- `quant validate <strategy>`
- `quant validate <strategy> --mode walk-forward`
- `quant validate <strategy> --mode perturbation`

### 建议新增输出指标

- `train_score`
- `validation_score`
- `test_score`
- `stability_score`
- `overfit_penalty`

当前状态（2026-05-06）：

- 第三阶段已经完成首版落地
- `quant validate <strategy>` 已可用
- 支持 `split` / `walk-forward` / `perturbation` / `all` 模式
- `split` 默认按交易日数量做 60/20/20 切分
- `walk-forward` 使用 expanding train + next test 窗口
- `perturbation` 基于 `optimization.search_space` 做单参数扰动
- 验证总结果已落盘到 `results/validation/...`
- 子回测可复用实验表保存和后续 `quant experiment ...` 复盘
- 后续默认从第四阶段 `quant evolve <strategy>` 开始推进

## 第四阶段：策略进化

目标：让系统基于优胜策略自动生成下一代候选策略。

### 进化操作

- 变异：微调阈值、替换排序字段、增删规则、扰动模式参数
- 交叉：组合两个优胜策略的规则或参数
- 淘汰：删除低分、不稳健、样本过少的策略

### 建议新增模块

- `src/quant/evolution/mutation.py`
- `src/quant/evolution/crossover.py`
- `src/quant/evolution/population.py`
- `src/quant/evolution/survivor_selection.py`

### 建议新增命令

- `quant evolve <strategy>`
- `quant evolve population --source generated/`
- `quant evolve run --generations 20`

### 第一版边界

第一版进化只允许在“已知字段、已知参数、已知模式空间”内变异，不做任意 SQL 自由生成。

当前状态（2026-05-06）：

- 第四阶段已经完成首版落地
- `quant evolve <strategy>` 已可用
- 首版基于 `optimization.search_space` 做单参数安全变异
- 每个变异候选会自动回测评分
- 低分候选会被淘汰，Top N 优胜策略会生成到 `strategies/generated/*.yml`
- 进化结果已落盘到 `results/evolution/...`
- 子回测可复用实验表保存和后续 `quant experiment ...` 复盘
- 交叉模块已具备基础能力，但 CLI 首版优先开放 mutation
- 后续默认从第五阶段 `quant discover ...` 开始推进

## 第五阶段：主动发现策略

目标：系统不只是优化已有策略，而是主动提出新的策略假设。

### 三条发现路径

#### 1. 样本反推

从历史高收益样本中反推启动前共同特征。

#### 2. 因子组合发现

在已有字段和模式参数中，自动组合条件并筛选有效子集。

#### 3. LLM 辅助假设生成

基于历史实验结果和高收益样本摘要，生成新的候选策略，再交给回测系统验证。

### 建议新增模块

- `src/quant/discovery/sample_mining.py`
- `src/quant/discovery/rule_mining.py`
- `src/quant/discovery/pattern_library.py`
- `src/quant/discovery/llm_generator.py`

### 建议新增命令

- `quant discover patterns`
- `quant discover rules`
- `quant discover llm`

当前状态（2026-05-07）：

- 第五阶段已经完成首版落地
- `quant discover filters <strategy>` 已可用
- 首版支持规则型策略的候选池特征导出、单字段分箱和 Top 过滤规则发现
- 发现产物写入 `results/discovery/...`
- Top 过滤规则会生成 `strategies/generated/*__discover_filter_rank_<n>.yml`
- 首版不做双字段组合、机器学习模型或自动上线
- 后续默认从发现策略的样本外验证和第六阶段 `quant autopilot run` 继续推进

## 第六阶段：自动调度与长期自迭代

目标：让系统定期自动运行优化、验证、发现和晋级流程。

### 调度建议

- 每日：增量更新数据
- 每周：重跑重点策略验证
- 每月：参数优化
- 每季度：主动发现和策略进化

### 建议新增模块

- `src/quant/autopilot/pipeline.py`
- `src/quant/autopilot/scheduler.py`
- `src/quant/autopilot/promotion.py`

### 建议新增命令

- `quant autopilot run`
- `quant autopilot daily`
- `quant autopilot weekly`

## 结果层与目录建议

建议新增实验与进化结果持久化结构。

### 数据表建议

- `experiment_run`
- `experiment_metric`
- `strategy_snapshot`
- `optimization_job`
- `optimization_trial`
- `evolution_generation`
- `evolution_individual`
- `discovery_candidate`

### 目录建议

- `strategies/base/`
- `strategies/generated/`
- `strategies/approved/`
- `results/experiments/`
- `results/optimization/`
- `results/evolution/`
- `results/discovery/`

## 推荐实施顺序

按投入产出比，建议优先级如下：

1. 实验结果持久化
2. 统一评分体系
3. `quant optimize <strategy>`
4. `quant validate <strategy>`
5. `quant evolve <strategy>`
6. `quant discover ...`
7. `quant autopilot run`

## 第一版暂不建议做的事

为了防止系统过快失控，第一版不要急着做：

- 任意 SQL 自由生成
- LLM 直接写策略并自动上线
- 无限维度大规模参数搜索
- 实盘交易自动执行

## 项目下一阶段的一句话定义

如果需要一句简洁定义来统一后续讨论，推荐使用：

“一个能自动优化、验证、进化和发现 A 股中短线策略的本地研究引擎。”

## 后续对话引用方式

以后如果在新对话中继续讨论，建议直接引用这几句话之一：

- “继续按照 `docs/strategy-evolution-roadmap.md` 往下推进”
- “基于策略自优化与自进化路线图，先做第一阶段”
- “按路线图继续拆第一期实施清单”

这样可以最大程度减少上下文丢失。
