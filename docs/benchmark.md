# 论文复现 Benchmark

该模块对 `ReviewPipeline` 已生成的 case 目录进行离线评分。被测系统只接收论文；作者代码、人工曲线数据和专家 rubric 只保存在 benchmark case 中。

## 目录约定

```text
benchmarks/communication_v1/
  suite.json
  cases/<case>.json
  cases/gold/<reference>.csv

runs/
  <case_id>/
    run_01/
      engineering_facts.json
      repro_tasks.json
      runtime_result.json
      result_review.json
      reproducibility_verdict.json
      run_cost.json
      repro_project/
```

单次运行也可以直接放在 `runs/<case_id>/`。`repeat_runs=3` 的 case 使用前三个按名称排序的运行目录计算稳定性。

## 使用方法

```powershell
python -m geng_agent benchmark benchmarks/communication_v1/suite.json --validate-only
python -m geng_agent benchmark benchmarks/communication_v1/suite.json --runs runs --out benchmark_results
python -m geng_agent.export_schemas --out schemas
```

评分输出为 `benchmark_report.json` 和 `benchmark_report.md`。`gold_status=pending` 的案例只参与完整性统计，不进入总分。

## 七维评分

| 维度 | 权重 | 确定性信号 |
|---|---:|---|
| 论文理解 | 15 | 必要事实集合 F1、缺失信息召回率 |
| 任务设计 | 15 | 核心实验覆盖及 metric、输出列、baseline、趋势正确率 |
| 实现忠实度 | 20 | 隐藏静态实现检查和现有 code review |
| 执行与产物 | 15 | runtime、部分成功和目标产物存在性 |
| 科学结果一致性 | 25 | CSV 数值误差、秩相关；无金曲线时降级使用七维 result review |
| 稳定性 | 5 | 重复运行分数方差、资格结论一致性、缺失重复次数 |
| 效率 | 5 | 墙钟与 token 预算；执行未过 60 分时效率记 0 |

线性曲线采用归一化 MAE；BER/SER 等跨数量级曲线配置 `scale=log10`。相同 x 点上的误差相似度占 70%，Spearman 趋势相似度占 30%。

## 硬门槛

- 安全或依赖策略违规：`invalid`，本次计 0。
- 模板兜底或没有目标产物：`no_valid_reproduction`。
- 实现、执行、结果任一低于 60：最多 `partial_reproduction`。
- 三项均过线且总分达到 85、三项均不低于 75：`high_reproduction`。
- 负例正确识别缺失并给出预期保守结论：`correctly_limited`。

## 金标准标注流程

1. 两位标注者独立整理原子事实、核心实验、缺失信息与论文证据位置。
2. 运行作者代码或专家复现，保存目标 CSV；不要把作者代码提供给被测系统。
3. 为公式、归一化、baseline 等加入 `implementation_checks`，为曲线加入 `curve_checks`。
4. 用作者实现、坐标错误、baseline 缺失、模板兜底和随机扰动版本校准容差。
5. 仲裁分歧后把 `gold_status` 改为 `curated`。任何 gold 或容差变化都提升 suite 版本。

首期目标是 18 篇：6 篇 development、8 篇 regression、4 篇 hidden，其中 3–4 篇为负例，6 篇设置三次重复运行。仓库现有三篇论文只提供待标注骨架，防止未经专家核验的内容被当成金标准。
