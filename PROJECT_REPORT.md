# 耿同学agent 项目架构报告（当前源码版）

> 更新日期：2026-07-10。本文档以当前源码、CLI 和测试为准。

## 1. 项目定位

耿同学agent 是面向通信论文的本地工程复现与证据审查系统。它把论文转换为可追溯事实、可运行任务、任务级复现代码、论文/本地图像证据和人工可读报告，不直接判断论文真伪。

当前核心原则：

- 前两阶段默认由 Codex analysis 子智能体完成，并由本地程序合并、去重、校验和查漏。
- 论文解析后先建立带实体 ID、子图、公式、表格与交叉引用的 Paper Memory，并用快照哈希锁定第三轮输入。
- `paper_thesis.json` 无条件抽取，向后续 writer 提供中心主张、机制、方法排序和适用区间。
- 第三阶段只使用任务级 Codex writer，不再保留全局 writer、harness runner、独立 reviewer 或模板项目路径。
- 一个任务对应一个 writer；writer 拥有代码编写、指定任务 full 运行、论文对比和最多 5 轮自我修正权。
- 主持人负责 Paper Memory、语义合并、实验可行性分级、sandbox、契约校验、依赖/安全检查、结构验收、产物合并和报告生成。

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
- `GENG_CODEX_MODEL`：项目子智能体模型覆盖；未设置时固定为 `gpt-5.6-luna`，与桌面 Codex 全局默认配置隔离。
- `GENG_CODEX_ANALYSIS_REASONING_EFFORT` / `GENG_CODEX_TASK_WRITER_REASONING_EFFORT`：默认分别为 `high` / `medium`，不继承桌面 `xhigh`。
- `GENG_CODEX_TASK_WRITER_MAX_CONCURRENCY`：自适应 writer 并发硬上限，默认 4；初始并发默认 2。
- `GENG_TASK_WRITER_GPU_FULL_SLOTS` / `GENG_TASK_WRITER_CPU_FULL_SLOTS`：本地 full 资源硬上限；不再限制代码编写阶段的 writer 数量。

OpenAI-compatible LLM 只保留为前两阶段显式兼容路径；第三阶段没有 LLM backend。

## 3. 当前流水线

1. **论文解析**
   - `documents.py` 读取 PDF/TXT/Markdown。
   - PDF 同时提取文本块并渲染页面 PNG。

2. **工程事实抽取**
   - 默认同时启动文本/公式专家与视觉/实验专家两个 Codex analysis 子智能体。
   - 本地程序按实体、子图、方法和实验条件做语义合并，补全字段并保留未决冲突。
   - gap finder 最多 6 轮，连续两轮无有效新增才停止。

3. **论文主张抽取**
   - 无条件生成 `paper_thesis.json`。
   - 记录中心结论、作用机制、方法排序、适用区间和 caveat。

4. **复现任务设计**
   - 默认同时启动两个 Codex analysis 子智能体。
   - 本地程序合并去重并校验 required facts、指标、趋势、baseline 和预期产物。
   - gap finder 循环到全覆盖/无新增或达到上限，默认 6。
   - `experiment_index.py` 生成 v2 实验索引，记录实体/子图、参数、baseline、验收标准和可复现性模式。

5. **任务级自治复现**
   - `agentic_task_writers.py` 为每个任务创建独立 sandbox。
   - 启动时探测 CPU、可用内存、GPU/显存和 Torch/CUDA；默认从 2 个 writer 开始，稳定后扩容，容量错误时减半并退避重试。
   - writer 先完成包含 backend 与资源申请的 `task_contract.json`，再修改本任务代码、私有配置、README 和 requirements。
   - 开启 `--run-repro` 时，guard 只允许运行被分配任务的 full，拒绝全项目 dispatcher 和其他任务。
   - 主持人内存 broker 统一核算 CPU、系统内存、GPU 编号和显存；writer 只访问自己 sandbox 内的认证通道，受信任 guard 对实际进程树执行资源上限。
   - writer 对照论文证据自审；不完全匹配时修改、重跑、再审查，最多 5 轮。
   - writer 交付契约、`task_agent_result.json/md`、本地图、论文目标图/定位图和 CSV/summary。
   - `proxy_only` 不允许冒充完全复现；失败进入任务级 failure memory。

6. **主持人验收与合并**
   - 恢复 trusted harness 文件，执行依赖策略、静态扫描、编译和结构验收。
   - 合并各任务代码与产物，不再重复全项目 full。
   - 汇总 `matched`、`explained_gap`、`failed`；仅分析范围/契约错误允许最多两次有界回流。

7. **报告输出**
   - `result_review.md/docx`：逐任务图像对比、结论、差异和 writer 自审附录。
   - `review.md/docx`：事实、任务、运行覆盖、风险和最终复现结论。
   - `runtime_result.json`、`risk_report.json`、`generated_files.json`、`run_cost.json`：内部审计与状态。

## 4. 关键模块

| 模块 | 当前职责 |
|---|---|
| `pipeline.py` | 主持人总编排、前两阶段分析、报告汇总 |
| `agentic_analysis.py` | Codex JSON analysis 子进程与 schema 重试 |
| `agentic_task_writers.py` | 一个任务一个自治 Codex writer |
| `task_writer_support.py` | sandbox、trusted 文件、证据包、manifest/cache 支撑 |
| `paper_evidence.py` | 任务相关事实/页面选择、论文图像编码、排序锚点 |
| `paper_memory.py` | 论文实体图、稳定 ID、交叉引用与快照 manifest |
| `semantic_merge.py` / `facts_coverage.py` | 语义合并、冲突保留、子图级覆盖与收敛检测 |
| `experiment_index.py` / `repro_feasibility.py` | 实验索引 v2 与可复现性模式分类 |
| `task_contract.py` | writer 开工前实验契约及稳定哈希 |
| `failure_memory.py` / `revision_router.py` | 失败指纹、去重记忆与有界上游回流 |
| `provenance.py` / `benchmark.py` | 自动化来源链与跨 case 离线评测 |
| `task_scripts.py` | task manifest、dispatcher 和 trusted scaffolding |
| `io_runtime.py` | writer 使用的可信产物 IO 与计算后端运行时 |
| `security.py` | 依赖 allowlist、静态扫描、环境隔离和脱敏 |
| `schema_models.py` / `schemas.py` | 当前结构化阶段的 Pydantic 合同 |
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
  engineering_facts.json
  paper_thesis.json
  repro_tasks.json
  experiment_index.json
  repro_project_manifest.json
  runtime_result.json
  failure_memory.jsonl
  revision_requests.json
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
  repro_project/
    requirements.txt
    configs/
    contracts/
    tasks/
    outputs/
```

## 6. 运行边界

- 论文内容、模型输出、writer 代码和运行日志均视为不可信输入。
- 第三方依赖必须通过 allowlist；缺失声明会被 reconciliation 和扫描器记录。
- full 只允许 writer 运行自己的任务，并受独立运行超时、CPU affinity、进程树内存、GPU slot/显存限制；资源排队和 full 时间不计入 Codex 活跃推理超时。
- 主持人只验证交付结构和安全边界，不替 writer 做第二次科学审查。
- `matched` 表示当前证据支持复现，不等同于论文真实性结论。
