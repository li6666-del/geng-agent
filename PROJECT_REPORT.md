# 耿同学agent 项目架构报告（当前源码版）

> 更新日期：2026-07-16。本文档以当前源码、CLI 和测试为准。

## 1. 项目定位

耿同学agent 是面向通信论文的本地工程复现与证据审查系统。它把论文转换为可追溯事实、可运行任务、任务级复现代码、论文/本地图像证据和人工可读报告，不直接判断论文真伪。

当前核心原则：

- 前两阶段采用字段级任务驱动证据闭环：全局事实抽取、初步任务设计、定向事实回补与任务刷新交替执行；按有效字段增量收敛，最多 6 轮。
- 论文解析后先建立带实体 ID、子图、公式、表格与交叉引用的 Paper Memory，并用快照哈希锁定第三轮输入。
- `paper_thesis.json` 无条件抽取，向后续 writer 提供中心主张、机制、方法排序和适用区间。
- 第三阶段使用任务级 Codex Writer 与对应的隔离 Task Reporter，不再保留全局 writer、harness runner、全局审查线程或模板项目路径。
- 一个任务对应一个 writer；所有 writer 同时启动并直接运行本任务 full，充分对照论文后提交 `ready_for_review`。
- 任务目标以论文原文和原图为准；实验索引只提供任务、参数、baseline 和证据定位导航。
- 每个任务配置一个独立 Codex task reporter，科学差异定向回流给对应 Writer；全部通过后才授予 `matched`，再由 Final Report Editor 生成报告。

## 2. 入口与配置

CLI 入口：

- `geng-agent review`：运行论文复现审查。
- `geng-agent status`：检查 case 产物和续跑位置。
- `geng-agent doctor`：检查 Python、编排依赖和复现白名单库。

Web 入口：`geng-agent-web`，默认监听 `127.0.0.1:8765`。

Codex 命令：

- `GENG_CODEX_CMD`：全局默认命令。
- `GENG_CODEX_ANALYSIS_CMD`：前两阶段覆盖命令。
- `GENG_CODEX_TASK_WRITER_CMD`：任务 writer 覆盖命令。
- `GENG_CODEX_TASK_REPORTER_CMD`：任务级科学审查 agent 覆盖命令。
- `GENG_CODEX_REPORT_EDITOR_CMD`：最终报告编辑 agent 覆盖命令。
- `GENG_CODEX_MODEL`：项目子智能体模型覆盖；未设置时固定为 `gpt-5.5`，与桌面 Codex 全局默认配置隔离。
- `GENG_CODEX_ANALYSIS_REASONING_EFFORT` / `GENG_CODEX_TASK_WRITER_REASONING_EFFORT` / `GENG_CODEX_TASK_REPORTER_REASONING_EFFORT` / `GENG_CODEX_REPORT_EDITOR_REASONING_EFFORT`：默认分别为 `high` / `medium` / `high` / `medium`。

OpenAI-compatible LLM 只保留为前两阶段显式兼容路径；第三阶段没有 LLM backend。

## 3. 当前流水线

1. **论文解析**
   - `documents.py` 读取 PDF/TXT/Markdown。
   - PDF 同时提取文本块并渲染页面 PNG。

2. **工程事实抽取**
   - 启动一个覆盖文本、公式、页面图像、表格和实验设置的 Codex 事实专家，生成高召回实验地图 `engineering_facts_initial.json`。
   - 本轮优先覆盖全部数值实验目标和核心模型、指标、算法、baseline，不为边缘事实反复扫描全文。
   - 视觉近似读数、附录公式、bound 和相互冲突的观察均保留来源、置信度与误差说明；程序只做结构规范化，不按内容类别删除事实。

3. **初步任务与定向回补**
   - Codex 任务设计专家生成 `repro_tasks_preliminary.json`。
   - 同一 figure/subfigure、同一指标、可共享仿真的曲线、baseline 和参数点优先合并成一个任务。
   - 每个任务用 `missing_fact_requests.required_fields` 精确声明会改变代码、配置或验收的缺失证据。
   - 程序按稳定请求键和字段 ID 合并去重；每轮事实专家只处理尚无终态的字段，并把搜索位置、证据和冲突写入累计台账。
   - 每轮结束后任务专家刷新公式链、参数矩阵、baseline、统计协议和图像锚点；只有新暴露的代码关键字段才触发下一轮，最多 6 轮。

4. **任务定稿与论文主张**
   - 初始事实与回补事实语义合并为最终 `engineering_facts.json`，未解析请求保留为显式缺失信息。
   - 任务专家根据最终事实定稿 `repro_tasks.json`；论文未公开或证据冲突的字段保留显式假设和敏感性检查，不伪装成已解决事实。
   - 无条件生成 `paper_thesis.json`，记录中心结论、作用机制、方法排序、适用区间和 caveat。
   - `experiment_index.py` 生成 v2 实验索引，只记录实体、参数、baseline、验收标准和证据缺口，不预测运行结果。

5. **任务级自治复现**
   - `agentic_task_writers.py` 为每个任务创建独立 sandbox。
   - 每个 sandbox 都包含原始论文、全论文页面图和前两轮最终定稿产物；全论文页面图直接发送给 writer，不再筛选任务页，任务相关事实摘要只作为文本导航。
   - 有几个复现任务就同时启动几个 writer，不按本机资源缩减并发。
   - writer 在独立 sandbox 内自主修改代码、配置、README 和 requirements，自行探测并选择 CPU/GPU。
   - 开启 `--run-repro` 时，writer 直接运行 smoke/full；主持人不拦截命令、不分配资源也不中途打断。
   - 抽取事实缺少参数时，writer 继续检索原论文 PDF、caption、公式、表格和附录；仍缺失时建立显式科学假设，并根据图像差异修正假设。
   - writer 逐项对照完整论文图像细节；定性趋势或方法排序一致不能作为候选终点，不完全匹配时继续修改、重跑、再审查。
   - Writer 采用直接循环：full 运行、逐项对照论文、提出具体修改方针、修改并再次 full；不设置固定轮数，无法继续时必须给出有证据的差异原因和停止依据。
   - 对适合批量矩阵或 Monte Carlo 的重计算，CUDA 可用时优先实现真实 Torch CUDA 路径；backend 标签本身不算 GPU 运行证据。
   - writer 提交 `ready_for_review` 时必须交付执行摘要、本地图和 CSV/summary；无效交付在同一 sandbox 自动续跑。
   - Writer 不能授予最终 matched；只有外部 Codex、网络、额度或运行环境错误可以中止候选生成。

6. **任务级闭环调度**
   - 每个 writer 完成可靠 full 后立即进入对应 task reporter，不等待其他任务。
   - 主持人只校验任务覆盖、运行证据路径和基础代码健康，不参与科学裁决。
   - task reporter 拒绝时只重启失败任务，已通过任务冻结；裁图或证据问题只重跑对应 reporter。

7. **任务级 Codex 审查与报告阶段**
   - 一个任务对应一个隔离 task reporter；它只读取本任务的 writer 产物、任务证据和完整论文。
   - task reporter 直接生成任务级 `accepted/revise` 裁决；科学差异只返回对应 writer，裁图或证据问题只重跑对应 reporter。
   - task reporter 为本任务定位并裁切准确的论文原图/子图；writer 不再生成 `paper_target_figure.json`。
   - 所有任务通过后，独立 Final Report Editor 只组织语言和排版，生成三份 Markdown；确定性 Word 渲染层只负责转为 DOCX。
   - `review.md/docx`：主审查报告。
   - `reproduction_report.md/docx`：逐任务关键参数、运行配置和假设。
   - `result_review.md/docx`：本地图与论文裁切图对比、结论、差异和原因，不包含 writer 自迭代附录。
   - `review.md/docx`：事实、任务、运行覆盖、风险和最终复现结论。
   - `runtime_result.json`、`risk_report.json`、`generated_files.json`、`run_cost.json`：内部审计与状态。

## 4. 关键模块

| 模块 | 当前职责 |
|---|---|
| `pipeline.py` | 主持人总编排、前两阶段分析、Writer/Task Reporter/Editor 阶段衔接 |
| `agentic_task_reporters.py` | 任务级隔离科学审查、论文图定位和裁切 |
| `agentic_report_editor.py` | 已验收任务包的三报告语言组织与排版 |
| `agentic_analysis.py` | Codex JSON analysis 子进程与 schema 重试 |
| `agentic_task_writers.py` | 一个任务一个自治 Codex writer |
| `task_writer_support.py` | sandbox、trusted 文件、证据包、manifest/cache 支撑 |
| `paper_evidence.py` | 任务相关事实/页面选择、论文图像编码、排序锚点 |
| `paper_memory.py` | 论文实体图、稳定 ID、交叉引用与快照 manifest |
| `semantic_merge.py` / `facts_coverage.py` | 语义合并、冲突保留和子图级确定性覆盖 |
| `task_evidence_backfill.py` | 字段级请求去重、证据引用验收、搜索台账和有效增量统计 |
| `targeted_backfill_loop.py` | 定向事实回补与任务刷新的最多六轮收敛编排 |
| `experiment_index.py` | 无预评级的实验实体、参数、baseline 与证据缺口索引 |
| `provenance.py` / `benchmark.py` | 自动化来源链与跨 case 离线评测 |
| `task_scripts.py` | task manifest、dispatcher 和 trusted scaffolding |
| `io_runtime.py` | writer 使用的可信产物 IO 与计算后端运行时 |
| `security.py` | 依赖 allowlist、静态扫描、环境隔离和脱敏 |
| `schema_models.py` / `schemas.py` | 当前结构化阶段的 Pydantic 接口定义 |
| `risk_report.py` / `verdict.py` | 风险维度与最终复现结论 |
| `docx_writer.py` / `review_markdown.py` | 人工可读 Markdown/Word 报告 |
| `web/` | 本地上传、后台运行、Codex 健康检查和阶段进度 |

已删除的旧模块：`agentic_project.py`、`runner.py`、`result_review.py`、`template_project.py`。其有效的证据处理和 JSON 重试能力已分别合并到 `paper_evidence.py`、`pipeline_helpers.py` 和 task-writer 支撑层。

## 5. 输出结构

```text
case_xxx/
  paper_chunks.json
  paper_memory.json
  memory_manifest.json
  engineering_facts_initial.json
  repro_tasks_preliminary.json
  engineering_facts_backfill.json
  engineering_facts.json
  paper_thesis.json
  repro_tasks.json
  experiment_index.json
  repro_project_manifest.json
  runtime_result.json
  report_assets/
  reproduction_report.md
  reproduction_report.docx
  automation_provenance.json
  result_review.md
  result_review.docx
  risk_report.json
  review.md
  review.docx
  audit/
    01_*
    02_*
    03c_task_writer_sandboxes/
    03c_task_writers_*.json
    04_reporter_*
    04_reporter_workspace/
  repro_project/
    requirements.txt
    configs/
    tasks/
    outputs/
```

## 6. 运行边界

- 论文内容、模型输出、writer 代码和运行日志均视为不可信输入。
- 第三方依赖与源码仍会被静态扫描；语法错误会阻止 runtime 通过，普通依赖警告保留在风险报告。
- writer 在独立 sandbox 内直接运行，主持人不设置 full 槽位、资源租约、进程限制或科学迭代超时。
- 主持人不替 Writer 或 task reporter 做主观科学审查；task reporter 直接以论文证据核验任务。
- `matched` 表示可观察目标经独立 task reporter 直接核验通过，不等同于论文真实性或作者代码等价结论。
