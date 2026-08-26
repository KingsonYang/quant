# quant

面向 A 股日线数据的本地研究与回测项目。当前主流程已经统一为“策略 YAML + `quant run ...` / `quant optimize ...` 命令”，同时支持规则型策略和模式型策略。

## 安装

优先使用 conda 环境：

```powershell
conda env create -f environment.yml
conda activate quant-backtest
python -m pip install -e .
```

## 推荐目录

- `strategies/`: 正式策略 YAML
- `strategies/base/`: 阶段性研究基准策略，承接更宽的优化搜索空间
- `strategies/generated/`: `quant optimize` 生成的派生策略
- `src/quant/config/`: 策略配置模型与加载
- `src/quant/commands/`: CLI 命令实现
- `src/quant/optimization/`: 参数优化能力
- `src/quant/validation/`: 稳健性验证能力
- `src/quant/evolution/`: 策略进化能力
- `src/quant/experiments/`: 实验平台与结果持久化
- `scripts/research/`: 实验性研究脚本
- `docs/strategy-config.md`: 策略配置说明
- `docs/strategy-evolution-roadmap.md`: 策略自优化与自进化路线图

## 基础工作流

本地工作台（数据维护、选股、策略回测）：

```powershell
quant web
```

浏览器打开 `http://127.0.0.1:8000`。数据页做 TDX 增量与日历同步，选股页查看某日候选，策略回测页看净值/回撤/交割，结束日往后可追加。

初始化数据库：

```powershell
quant init-db
```

导入和更新数据：

```powershell
quant ingest-baidu-all-incremental
quant build-trading-calendar-incremental
```

TDXQuant 数据源（作为 baidu/ths 的补充，写入原表 daily_bar）：

```powershell
quant ingest-tdxquant                            # 增量最近 5 天（主板）
quant ingest-tdxquant --start 2026-06-20 --end 2026-06-23
quant ingest-tdxquant --stock 600000.SH --days 5
quant ingest-tdxquant --test-only --stock 600000.SH
```

> TDXQuant 作为增量数据源，只对 baidu/ths 都没覆盖的日期写入，**不修改原表的 baidu/ths 数据**。
> 数据源标识：`source_name='tdxquant'`，`parser_version='tdxquant-get-market-data-v1'`。
> 单位与 baidu/ths 一致：volume=股 / amount=元（TDXQuant 万元已 ×10000）/ change_pct=% / turnover=%。

查看策略：

```powershell
quant strategy list
quant strategy show amount_ratio_prev_5d_near_2x
quant strategy show shandian_1hao_erci_baoliang_qidong
quant strategy show limit_up_pullback_ma5_reclaim
```

按策略选股：

```powershell
quant run select amount_ratio_prev_5d_near_2x --trade-date 2026-04-20
quant run select shandian_1hao_erci_baoliang_qidong --trade-date 2026-04-21
quant run select limit_up_pullback_ma5_reclaim --trade-date 2026-05-06
```

普通回测：

```powershell
quant run backtest-selection amount_ratio_prev_5d_near_2x --start-date 2026-04-01 --end-date 2026-04-14
quant run backtest-selection shandian_1hao_erci_baoliang_qidong --start-date 2025-12-31 --end-date 2026-04-21
quant run backtest-selection limit_up_pullback_ma5_reclaim --start-date 2025-12-31 --end-date 2026-05-06
```

策略回测：

```powershell
quant run backtest-strategy amount_ratio_prev_5d_near_2x --start-date 2024-12-30 --end-date 2026-04-20
quant run backtest-strategy shandian_1hao_erci_baoliang_qidong --start-date 2025-12-31 --end-date 2026-04-21
quant run backtest-strategy limit_up_pullback_ma5_reclaim --start-date 2025-12-31 --end-date 2026-05-06
```

持有期矩阵回测：

```powershell
quant run matrix strategy-holding-periods amount_ratio_prev_5d_near_2x --start-date 2024-12-30 --end-date 2026-04-20 --holding-periods 1,2,3,4,5
quant run matrix strategy-holding-periods shandian_1hao_erci_baoliang_qidong --start-date 2025-12-31 --end-date 2026-04-21 --holding-periods 1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20
quant run matrix strategy-holding-periods limit_up_pullback_ma5_reclaim --start-date 2025-12-31 --end-date 2026-05-06 --holding-periods 1,2,3,4,5
```

汇总矩阵结果：

```powershell
quant summarize strategy-holding-periods --results-dir results/strategy_holding_periods/shandian_1hao_erci_baoliang_qidong_2025-12-31_2026-04-21
```

## 实验平台

`quant run backtest-selection ...` 和 `quant run backtest-strategy ...` 默认会自动保存实验记录、指标、评分和 artifact 到 `results/experiments/run_<run_id>/`。

```powershell
quant run backtest-selection amount_ratio_prev_5d_near_2x --start-date 2026-04-01 --end-date 2026-04-14 --run-name amount_ratio_apr_window
quant run backtest-strategy shandian_1hao_erci_baoliang_qidong --start-date 2025-12-31 --end-date 2026-04-21
quant experiment list
quant experiment show 1
quant experiment compare 1 2
```

如果只想看结果、不想保存实验，可以加：

```powershell
quant run backtest-selection amount_ratio_prev_5d_near_2x --start-date 2026-04-01 --end-date 2026-04-14 --no-save-experiment
```

## 参数优化

第二阶段已经接入 `quant optimize <strategy>`，可以基于 YAML 里的 `optimization.search_space` 自动搜索更优参数组合。正式策略可以放在 `strategies/`，更宽的研究型搜索空间建议放在 `strategies/base/`。

```powershell
quant optimize amount_ratio_prev_5d_near_2x --start-date 2026-04-01 --end-date 2026-04-14
quant optimize shandian_1hao_erci_baoliang_qidong --target selection_backtest --method random --max-trials 20 --seed 7 --start-date 2025-12-31 --end-date 2026-04-21
python -m quant optimize amount_ratio_prev_5d_near_2x_phase2 --strategies-dir strategies/base --method random --max-trials 20 --seed 7 --target strategy_backtest --start-date 2026-04-01 --end-date 2026-05-06 --top-n 3
python -m quant optimize shandian_1hao_erci_baoliang_qidong_phase2 --strategies-dir strategies/base --method random --max-trials 20 --seed 7 --target selection_backtest --start-date 2025-12-31 --end-date 2026-05-06 --top-n 3
```

优化结果会产出：

- `results/optimization/.../summary.json`
- `results/optimization/.../trials.csv`
- `results/optimization/.../top_trials.json`
- `strategies/generated/*.yml`

生成出来的派生策略可以继续验证和回测：

```powershell
quant strategy validate --strategies-dir strategies/generated
quant run backtest-strategy amount_ratio_prev_5d_near_2x_phase2__strategy_backtest__rank_1 --strategies-dir strategies/generated --start-date 2026-04-01 --end-date 2026-05-06
```

## 稳健性验证

第三阶段接入 `quant validate <strategy>`，用于对候选策略做训练/验证/测试切分、walk-forward 和参数扰动稳定性检查。

```powershell
quant validate amount_ratio_prev_5d_near_2x_phase2__strategy_backtest__rank_1 --strategies-dir strategies/generated --target strategy_backtest --start-date 2026-04-01 --end-date 2026-05-06
quant validate shandian_1hao_erci_baoliang_qidong_phase2__selection_backtest__rank_1 --strategies-dir strategies/generated --target selection_backtest --mode walk-forward --start-date 2025-12-31 --end-date 2026-05-06
quant validate amount_ratio_prev_5d_near_2x_phase2__strategy_backtest__rank_1 --strategies-dir strategies/generated --target strategy_backtest --mode perturbation --max-perturbations 20 --seed 7 --start-date 2026-04-01 --end-date 2026-05-06
```

验证结果会产出：

- `results/validation/.../summary.json`
- `results/validation/.../split_segments.csv`
- `results/validation/.../walk_forward.csv`
- `results/validation/.../perturbations.csv`

## 策略进化

第四阶段接入 `quant evolve <strategy>`，基于策略 YAML 的 `optimization.search_space` 在已知参数空间内做安全变异、回测评分、淘汰低分候选，并把优胜策略写入 `strategies/generated/`。

```powershell
quant evolve amount_ratio_prev_5d_near_2x_phase2__strategy_backtest__rank_1 --strategies-dir strategies/generated --target strategy_backtest --max-mutations 20 --top-n 5 --seed 7 --start-date 2026-04-01 --end-date 2026-05-06
quant evolve shandian_1hao_erci_baoliang_qidong_phase2__selection_backtest__rank_1 --strategies-dir strategies/generated --target selection_backtest --max-mutations 20 --top-n 5 --seed 7 --start-date 2025-12-31 --end-date 2026-05-06
```

进化结果会产出：

- `results/evolution/.../summary.json`
- `results/evolution/.../candidates.csv`
- `results/evolution/.../survivors.json`
- `strategies/generated/*__evolve_gen_<n>__*.yml`

## 主动发现过滤规则

第五阶段首版接入 `quant discover filters <strategy>`，用于把规则型策略每天选出的候选池补齐特征、按单字段分箱统计胜率和收益，并把表现更好的过滤区间生成候选策略。

```powershell
quant discover filters amount_ratio_prev_5d_near_2x__selection_backtest__rank_1 --strategies-dir strategies/generated --target selection_backtest --start-date 2022-12-29 --end-date 2026-05-06 --top-rules 5 --bins 5 --min-samples 50 --seed 7
quant discover filters shandian_1hao_erci_baoliang_qidong --target selection_backtest --start-date 2022-12-29 --end-date 2026-05-06 --top-rules 5 --bins 5 --min-samples 30 --seed 7
quant strategy validate --strategies-dir strategies/generated
quant validate shandian_1hao_erci_baoliang_qidong__discover_filter_rank_1 --strategies-dir strategies/generated --target selection_backtest --mode split --start-date 2022-12-29 --end-date 2026-05-06 --seed 7
quant validate amount_ratio_prev_5d_near_2x__selection_backtest__rank_1__discover_filter_rank_1 --strategies-dir strategies/generated --target selection_backtest --mode all --start-date 2022-12-29 --end-date 2026-05-06 --walk-folds 5 --max-perturbations 20 --seed 7
```

主要输出：

- `results/discovery/.../candidate_features.csv`
- `results/discovery/.../feature_bins.csv`
- `results/discovery/.../candidate_rules.json`
- `results/discovery/.../summary.json`
- `strategies/generated/*__discover_filter_rank_<n>.yml`

`pattern_sql` 策略的发现结果会保留原模式定义，并把二级过滤条件追加到 `selection.filter_rules`；运行时顺序是“模式命中 -> filter_rules 过滤 -> selection_top_n”。

## 策略 YAML

策略文件放在 `strategies/*.yml`，一个文件对应一个策略名。完整说明见 [docs/strategy-config.md](docs/strategy-config.md)。

项目后续演进路线已经固化到 [docs/strategy-evolution-roadmap.md](docs/strategy-evolution-roadmap.md)。如果后面开新对话继续讨论，可以直接引用这份文档。

## Legacy 命令

以下命令继续兼容，但不再作为主推荐路径：

- `quant select-stocks`
- `quant backtest-selection`
- `quant backtest-strategy`

迁移对照：

- `quant select-stocks ... --rules-file ...` -> `quant run select <strategy> --trade-date ...`
- `quant backtest-selection ... --rules-file ...` -> `quant run backtest-selection <strategy> --start-date ... --end-date ...`
- `quant backtest-strategy ... --rules-file ...` -> `quant run backtest-strategy <strategy> --start-date ... --end-date ...`

研究脚本兼容入口：

```powershell
python scripts/research/backtest_pattern_selection_hps.py 2025-12-31 2026-04-21 data/quant.duckdb --hps 1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20 --k0 2.0 --k1 1.5
```

推荐迁移到：

```powershell
quant run matrix strategy-holding-periods shandian_1hao_erci_baoliang_qidong --start-date 2025-12-31 --end-date 2026-04-21 --holding-periods 1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20
```
