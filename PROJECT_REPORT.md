# 耿同学agent 项目架构报告（当前源码版）

> 更新日期：2026-07-16。本文档以当前源码、CLI 和测试为准。

## 1. 项目定位

耿同学agent 是面向通信论文的本地工程复现与证据审查系统。它把论文转换为可追溯事实、可运行任务、任务级复现代码、论文/本地图像证据和人工可读报告，不直接判断论文真伪。

当前核心原则：

- 前两阶段采用前置软交接：一轮全局事实抽取、一轮初步任务设计后立即判断是否可交给 Writer；只有明确选中的实验定义 blocker 才进入定向回补，通常 0–2 轮，第三轮仅作异常熔断。
- 论文解析后先建立带实体 ID、子图、公式、表格与交叉引用的 Paper Memory，并用快照哈希锁定第三轮输入。
- `paper_thesis.json` 无条件抽取，向后续 writer 提供中心主张、机制、方法排序和适用区间。
- 第三阶段使用任务级 Codex Writer 与对应的隔离 Task Reporter，不再保留全局 writer、harness runner、全局审查线程或模板项目路径。
- 一个任务对应一个 writer；所有 writer 同时启动并直接运行本任务 full，把论文明确事实作为最高约束，只在论文空白处作显式假设，核心观点得到支持后提交 `ready_for_review`。
- 任务目标以论文原文和原图为准；实验索引只提供任务、参数、baseline 和证据定位导航。
- 每个任务配置一个独立 Codex task reporter，按材料性门槛把明确事实冲突或核心观点失败定向回流给对应 Writer；合理假设和非材料差异不阻断通过，全部通过后再授予 `matched` 并生成报告。

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
- `GENG_CODEX_MODEL`：项目子智能体模型覆盖；未设置时固定为 `gpt-5.6-sol`，与桌面 Codex 全局默认配置隔离。
- `GENG_CODEX_ANALYSIS_REASONING_EFFORT` / `GENG_CODEX_TASK_WRITER_REASONING_EFFORT` / `GENG_CODEX_TASK_REPORTER_REASONING_EFFORT` / `GENG_CODEX_REPORT_EDITOR_REASONING_EFFORT`：默认均为 `xhigh`。

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
   - 程序按稳定请求键和字段 ID 合并去重；初步任务专家先输出 `backfill_handoff`，事实专家只处理其中明确选择的 blocker。
   - 每轮结束后任务专家再次软交接；同一未解字段最多搜索两次，第三轮只容纳新暴露的 blocker，不按“无新增事实”反复空转。

4. **任务定稿与论文主张**
   - 初始事实与回补事实语义合并为最终 `engineering_facts.json`，未解析请求保留为显式缺失信息。
   - Python 仅硬验收 JSON 和基本结构；来源、事实引用、证据类型、缺失 resolution、assumption 与 sensitivity_check 均写入 `analysis_warnings.json`，不阻断 Writer。
   - 无条件生成 `paper_thesis.json`，记录中心结论、作用机制、方法排序、适用区间和 caveat。
   - `experiment_index.py` 生成 v2 实验索引，只记录实体、参数、baseline、验收标准和证据缺口，不预测运行结果。
   - 新案例由 Architecture Agent 生成 `scientific_architecture.json`，跨文档校验 task/experiment/component/quantity 引用以及共享作用域。
   - Foundation Writer 顺序生成共享 `src/` 和契约测试，验证通过后保存内容寻址快照；并行 task writer 不得修改共享层。

5. **任务级自治复现**
   - `agentic_task_writers.py` 为每个任务创建独立 sandbox。
   - v2 sandbox 先安装完全相同的 Foundation snapshot；其哈希参与 writer cache key，Foundation 改变时旧任务结果自动失效。
   - 最终项目从 canonical Foundation 与任务依赖闭包组装，并重新执行必需文件、编译和本地导入门禁。
   - 每个 sandbox 都包含原始论文、全论文页面图和前两轮最终定稿产物；全论文页面图直接发送给 writer，不再筛选任务页，任务相关事实摘要只作为文本导航。
   - 有几个复现任务就同时启动几个 writer，不按本机资源缩减并发。
   - writer 在独立 sandbox 内自主修改代码、配置、README 和 requirements，自行探测并选择 CPU/GPU。
   - 开启 `--run-repro` 时，writer 直接运行 smoke/full；主持人不拦截命令、不分配资源也不中途打断。
   - 抽取事实缺少参数时，writer 继续检索原论文 PDF、caption、公式、表格和附录；仍缺失时才建立显式科学假设。假设可补全未说明的值或实现步骤，但不得覆盖论文明确的数据、模型、公式或核心算法。
   - writer 首先核验明确事实和任务核心观点，再把剩余问题区分为材料性 blocker、合理的论文空白假设和非材料差异；只有材料性 blocker 才继续修改和 full。
   - Writer 采用直接循环：full 运行、证据分级、提出具体修改方针、修改并再次 full；不设置固定轮数，也不盲从与论文冲突的 Reporter 建议或为非阻塞差异机械重跑。
   - 对适合批量矩阵或 Monte Carlo 的重计算，CUDA 可用时优先实现真实 Torch CUDA 路径；backend 标签本身不算 GPU 运行证据。
   - writer 提交 `ready_for_review` 时必须交付执行摘要、本地图和 CSV/summary；无效交付在同一 sandbox 自动续跑。
   - Writer 不能授予最终 matched；只有外部 Codex、网络、额度或运行环境错误可以中止候选生成。

6. **任务级闭环调度**
   - 每个 writer 完成可靠 full 后立即进入对应 task reporter，不等待其他任务。
   - 主持人只校验任务覆盖、运行证据路径和基础代码健康，不参与科学裁决。
   - task reporter 拒绝时只重启失败任务，已通过任务冻结；裁图或证据问题只重跑对应 reporter。

7. **任务级 Codex 审查与报告阶段**
   - 一个任务对应一个隔离 task reporter；它只读取本任务的 writer 产物、任务证据和完整论文。
   - task reporter 直接生成任务级 `accepted/revise` 裁决；明确事实被实质违反或核心观点未获支持，且存在论文证据支撑的可执行修改时才返回对应 writer。合理、公开的论文空白假设可有条件通过，裁图或证据问题只重跑对应 reporter。
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
| `agentic_analysis.py` | Codex analysis 子进程与最多一次、零论文图片的格式专修 |
| `agentic_task_writers.py` | 一个任务一个自治 Codex writer |
| `task_writer_support.py` | sandbox、trusted 文件、证据包、manifest/cache 支撑 |
| `paper_evidence.py` | 任务相关事实/页面选择、论文图像编码、排序锚点 |
| `semantic_merge.py` / `facts_coverage.py` | 语义合并、冲突保留和子图级确定性覆盖 |
| `task_evidence_backfill.py` | 字段级请求去重、部分验收、软诊断、搜索台账和有限重搜 |
| `targeted_backfill_loop.py` | 前置软交接、明确 blocker 的选择性回补和三轮异常熔断 |
| `experiment_index.py` | 无预评级的实验实体、参数、baseline 与证据缺口索引 |
| `provenance.py` / `benchmark.py` | 自动化来源链与跨 case 离线评测 |
| `task_scripts.py` | task manifest、dispatcher 和 trusted scaffolding |
| `io_runtime.py` | writer 使用的可信产物 IO 与计算后端运行时 |
| `security.py` | 依赖 allowlist、静态扫描、环境隔离和脱敏 |
| `schema_models.py` / `schemas.py` | 当前结构化阶段的 Pydantic 接口定义 |
| `risk_report.py` / `verdict.py` | 风险维度与最终复现结论 |
| `docx_writer.py` / `review_markdown.py` | 人工可读 Markdown/Word 报告 |
| `web/` | 本地上传、后台运行、Codex 健康检查和阶段进度 |

已删除的旧编排与语义中间层不再参与当前流程。证据链现在直接使用原论文、全文分块、最终分析产物和图候选索引；缓存仅依据文件内容哈希失效。

## 5. 输出结构

所有 case 默认统一保存在 %USERPROFILE%\Desktop\耿同学agent_cases；相对 CLI case 名称也从这里解析，避免运行产物污染源码仓库。

```text
case_xxx/
  paper_chunks.json
  engineering_facts_initial.json
  repro_tasks_preliminary.json
  engineering_facts_backfill.json
  engineering_facts.json
  analysis_warnings.json
  paper_thesis.json
  repro_tasks.json
  experiment_index.json
  scientific_architecture.json
  foundation_manifest.json
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
- 主持人不替 Writer 或 task reporter 做主观科学审查；task reporter 只按明确事实忠实度、核心观点支持度和材料性门槛核验任务。
- `matched` 表示论文明确事实未被违反、核心观点得到本地结果支持且假设已透明记录，不等同于论文真实性或作者代码等价结论。
