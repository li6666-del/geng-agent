# 耿同学agent 项目架构报告（当前源码版）

> 更新日期：2026-07-11。本文档以当前源码、CLI 和测试为准。

## 1. 项目定位

耿同学agent 是面向通信论文的本地工程复现与证据审查系统。它把论文转换为可追溯事实、可运行任务、任务级复现代码、论文/本地图像证据和人工可读报告，不直接判断论文真伪。

当前核心原则：

- 第一阶段由一个事实专家迭代抽取，第二阶段由一个任务设计专家迭代拆解；每轮都读取上一轮合并结果，首轮无新增即停止。
- 论文解析后先建立带实体 ID、子图、公式、表格与交叉引用的 Paper Memory，并用快照哈希锁定第三轮输入。
- `paper_thesis.json` 无条件抽取，向后续 writer 提供中心主张、机制、方法排序和适用区间。
- 第三阶段只使用任务级 Codex writer，不再保留全局 writer、harness runner、独立 reviewer 或模板项目路径。
- 一个任务对应一个 writer；所有 writer 同时启动，写完代码即申请本任务 full，并持续迭代到匹配或形成有证据的差异解释。
- 主持人不做交付格式验收或修复，不因 BOM、字段、路径、JSON 或合同格式二次唤醒 writer；最终只收集任务结果。
- 全部 writer 完成后只启动一个 Codex reporter，集中负责论文原图定位/裁切、报告语言与排版。

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
- `GENG_CODEX_REPORTER_CMD`：最终报告 agent 覆盖命令。
- `GENG_CODEX_MODEL`：项目子智能体模型覆盖；未设置时固定为 `gpt-5.5`，与桌面 Codex 全局默认配置隔离。
- `GENG_CODEX_ANALYSIS_REASONING_EFFORT` / `GENG_CODEX_TASK_WRITER_REASONING_EFFORT` / `GENG_CODEX_REPORTER_REASONING_EFFORT`：默认分别为 `high` / `medium` / `high`。
- `GENG_TASK_WRITER_GPU_FULL_SLOTS` / `GENG_TASK_WRITER_CPU_FULL_SLOTS`：本地 full 资源硬上限；不再限制代码编写阶段的 writer 数量。

OpenAI-compatible LLM 只保留为前两阶段显式兼容路径；第三阶段没有 LLM backend。

## 3. 当前流水线

1. **论文解析**
   - `documents.py` 读取 PDF/TXT/Markdown。
   - PDF 同时提取文本块并渲染页面 PNG。

2. **工程事实抽取**
   - 启动一个覆盖文本、公式、页面图像、表格和实验设置的 Codex 事实专家。
   - 本地程序按实体、子图、方法和实验条件做语义合并，补全字段并保留未决冲突。
   - 把合并后的事实继续交给同一专家查漏，首轮语义新增为 0 时停止，不设默认轮数上限。

3. **论文主张抽取**
   - 无条件生成 `paper_thesis.json`。
   - 记录中心结论、作用机制、方法排序、适用区间和 caveat。

4. **复现任务设计**
   - 启动一个 Codex 任务设计专家，并把每轮合并后的任务继续作为下一轮输入。
   - 同一 figure/subfigure、同一指标、可共享仿真的曲线、baseline 和参数点优先合并成一个任务。
   - 首轮没有语义新增任务时停止；程序覆盖率只作为专家输入，不再提前跳过确认轮。
   - `experiment_index.py` 生成 v2 实验索引，记录实体/子图、参数、baseline、验收标准和可复现性模式。

5. **任务级自治复现**
   - `agentic_task_writers.py` 为每个任务创建独立 sandbox。
   - 有几个复现任务就同时启动几个 writer；CPU、内存、GPU/显存和 Torch/CUDA 探测只控制 full 资源租约。
   - writer 先完成包含 backend 与资源申请的 `task_contract.json`，再修改本任务代码、私有配置、README 和 requirements。
   - 开启 `--run-repro` 时，guard 只允许运行被分配任务的 full，拒绝全项目 dispatcher 和其他任务。
   - 主持人内存 broker 统一核算 CPU、系统内存、GPU 编号和显存；writer 只访问自己 sandbox 内的认证通道，受信任 guard 对实际进程树执行资源上限。
   - writer 对照论文证据自审；不完全匹配时继续修改、重跑、再审查，没有轮数上限。
   - writer 交付契约、`task_agent_result.json/md`、本地图、论文目标图/定位图和 CSV/summary。
   - `proxy_only` 不允许冒充完全复现；writer 必须在本次任务交付中说明代理边界与差异原因。

6. **结果收集**
   - 恢复 trusted harness 边界并收集各 writer 的代码、运行记录、图片和自审结论，不重复全项目 full。
   - 不执行主持人格式 repair、结构 gate 或上游修订回流。
   - 汇总 writer 自报的 `matched`、`explained_gap`、`failed`，计算风险与 reproducibility verdict。

7. **Codex 报告阶段**
   - 单个 reporter 读取所有任务 contract、writer 最终结果、本地图片和论文页。
   - reporter 为每个任务定位并裁切准确的论文原图/子图；writer 不再生成 `paper_target_figure.json`。
   - reporter 生成三份 Markdown，确定性 Word 渲染层只负责转为 DOCX。
   - `review.md/docx`：主审查报告。
   - `reproduction_report.md/docx`：逐任务关键参数、运行配置和假设。
   - `result_review.md/docx`：本地图与论文裁切图对比、结论、差异和原因，不包含 writer 自迭代附录。
   - `review.md/docx`：事实、任务、运行覆盖、风险和最终复现结论。
   - `runtime_result.json`、`risk_report.json`、`generated_files.json`、`run_cost.json`：内部审计与状态。

## 4. 关键模块

| 模块 | 当前职责 |
|---|---|
| `pipeline.py` | 主持人总编排、前两阶段分析、writer 与 reporter 阶段衔接 |
| `agentic_reporter.py` | 单 Codex 报告沙盒、论文图裁切和三报告交付 |
| `agentic_analysis.py` | Codex JSON analysis 子进程与 schema 重试 |
| `agentic_task_writers.py` | 一个任务一个自治 Codex writer |
| `task_writer_support.py` | sandbox、trusted 文件、证据包、manifest/cache 支撑 |
| `paper_evidence.py` | 任务相关事实/页面选择、论文图像编码、排序锚点 |
| `paper_memory.py` | 论文实体图、稳定 ID、交叉引用与快照 manifest |
| `semantic_merge.py` / `facts_coverage.py` | 语义合并、冲突保留、子图级覆盖与收敛检测 |
| `experiment_index.py` / `repro_feasibility.py` | 实验索引 v2 与可复现性模式分类 |
| `task_contract.py` | writer 开工前实验契约及稳定哈希 |
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
    contracts/
    tasks/
    outputs/
```

## 6. 运行边界

- 论文内容、模型输出、writer 代码和运行日志均视为不可信输入。
- 第三方依赖仍受 allowlist 和运行 guard 约束；汇总阶段的扫描结果只进入报告，不触发格式 repair。
- full 只允许 writer 运行自己的任务，并受独立运行超时、CPU affinity、进程树内存、GPU slot/显存限制；资源排队和 full 时间不计入 Codex 活跃推理超时。
- 主持人不验证交付格式，也不替 writer 做第二次科学审查；writer 对最终内容和格式负责。
- `matched` 表示当前证据支持复现，不等同于论文真实性结论。
