# 耿同学 agent

面向通信论文的自动复现与可信审查工具。它不是“论文真伪裁判”，而是把一篇通信论文拆成可追溯事实、可运行任务、任务级复现实验、图像证据对比和人工可读报告，帮助研究者更快判断复现结果是否支持论文结论，以及差异可能来自哪里。

当前项目生成与复现主线只使用 Codex CLI；OpenAI-compatible LLM 仅保留为前两阶段论文分析的显式兼容选项。

## 当前能力

- 解析 PDF/TXT/Markdown 论文，构建带稳定实体 ID、子图、公式、表格、章节和交叉引用的 `paper_memory.json`。
- 由两个异构 Codex analysis 子智能体并行抽取工程事实，本地语义合并会补全字段、保留冲突，并在连续两轮无有效新增后停止。
- 无条件抽取论文核心主张、作用机制、方法排序和限制，生成 `paper_thesis.json` 作为后续复现锚点。
- 将论文图表拆成可运行的复现实验任务；`experiment_index.json` 记录目标实体、子图、参数、baseline、验收标准和 `native_full/scaled_full/proxy_only/blocked` 可复现性模式。
- 第三阶段采用任务级自治 writer：一个任务一个 Codex writer，独立写代码；传 `--run-repro` 时通过 guard 跑本任务 full、对照论文图、自我修正，最多 5 轮。
- 不再启动独立 reviewer；每个 writer 自己给出 `matched`、`explained_gap` 或 `failed`，主持人只做结构验收、依赖/安全检查、产物合并和报告生成。
- writer full 前必须完成 `task_contract.json`；Python guard 校验契约并把契约哈希写入可信运行记录。失败会进入去重的 `failure_memory.jsonl`，仅分析范围或契约错误允许最多两次上游修订回流。
- 生成主报告 `review.md/docx`、结果对比报告 `result_review.md/docx`、运行结果、风险报告和完整 audit 证据链。
- 提供极简 Web UI，可上传 PDF 或填写 PDF 链接并实时查看阶段进度。

## 工作流

```text
论文 PDF
  -> 文本、页面图像、图表位置解析
  -> 构建 paper_memory.json 与 memory_manifest.json
  -> 两个异构 Codex analysis 抽取 engineering_facts.json
  -> 本地语义合并、冲突保留、source 回指校验、连续两轮无新增停止
  -> Codex analysis 抽取 paper_thesis.json
  -> Codex analysis 生成 repro_tasks.json
  -> 本地任务引用、子图、指标、趋势、baseline 与覆盖率校验
  -> experiment_index v2 可复现性分级
  -> 为每个复现任务创建独立 sandbox
  -> 每个 writer 先完成 task_contract，再写代码、运行本任务 full 并自审对比论文
  -> Python guard 限制 writer 只能运行自己的任务，校验 contract 并记录可信运行日志
  -> 失败记忆去重；必要时有界回流修订任务分析
  -> 主持人合并任务代码、图片、CSV/summary、自审结论和审计材料
  -> 输出 result_review.md/docx、risk_report.json、review.md/docx
```

## 安装

要求 Python 3.11+。建议使用已经装好科学计算/GPU 依赖的环境。

```bash
git clone https://github.com/li6666-del/geng-agent.git
cd geng-agent

# 安装 CLI + 通信论文复现常用依赖
python -m pip install -e ".[repro]"

# 如需 Web UI
python -m pip install -e ".[repro,web]"
```

`.[repro]` 会安装生成复现代码常用且允许的依赖，包括 `numpy`、`scipy`、`matplotlib`、`pandas`、`sympy`、`numba`、`torch`、`scikit-learn`、`galois`、`h5py` 等。依赖白名单的真源在 `geng_agent/security.py`。

安装后先自检：

```bash
python -m geng_agent doctor
```

`doctor` 会检查 Python 版本、运行本体依赖和复现白名单库。缺关键库时请先修复，再喂论文。

## 论文目标图定位

系统会为每个复现任务准备论文文本、相关页截图和事实证据；第三阶段的 task writer 负责从这些证据中定位目标图或子图，并交付面向报告的论文侧图片。

writer 必须生成 `paper_target_figure.json`，至少记录 `target_figure`、`source_page`、`confidence`、`contains_only_target`、`fallback_used`、`reason` 和 `paper_image_paths`，并在 `task_agent_result.json.paper_image_paths` 中列出自己创建的 `outputs/<task>/paper_target_crop.png` 或带红框的 `outputs/<task>/paper_target_locator.png`。主持人只做结构验收和报告合并，不再依赖外部图表抽取工具，也不会把裸 `paper_page_*.png` 整页截图当作最终论文原图。

## Codex 配置

默认全流程走 Codex CLI：

```bash
set GENG_CODEX_CMD=codex
```

也可以按阶段覆盖：

```bash
set GENG_CODEX_ANALYSIS_CMD=codex
set GENG_CODEX_TASK_WRITER_CMD=codex
```

项目启动的 Codex 子智能体默认使用 `gpt-5.6-luna`，不跟随桌面 Codex 的全局默认模型。需要临时覆盖时可设置：

```bash
set GENG_CODEX_MODEL=gpt-5.6-sol
```

analysis 子智能体默认使用 `high` 推理强度，task writer 默认使用 `medium`，避免继承桌面配置中的 `xhigh` 后出现不必要的超长推理。可分别覆盖：

```bash
set GENG_CODEX_ANALYSIS_REASONING_EFFORT=high
set GENG_CODEX_TASK_WRITER_REASONING_EFFORT=medium
```

task writer 默认采用资源感知并发：所有任务先进入队列，初始同时运行 2 个 writer；连续稳定完成后逐步增加，默认最多 4 个；出现 Codex rate-limit/model-capacity 时并发减半，并对整个 writer 队列执行统一冷却后保留 sandbox 重试。writer 推理并发与本地 full 并发彼此独立。常用硬上限：

```bash
set GENG_CODEX_TASK_WRITER_MAX_CONCURRENCY=4
set GENG_TASK_WRITER_GPU_FULL_SLOTS=1
set GENG_TASK_WRITER_CPU_FULL_SLOTS=2
set GENG_RESOURCE_RAM_RESERVE_GB=4
```

兼容变量 `GENG_CODEX_TASK_WRITER_CONCURRENCY` 会把 writer 并发固定为指定值。每次运行都会重新探测 CPU、可用内存、GPU、显存和 Torch/CUDA，并写入 `audit/hardware_snapshot.json`、`audit/resource_plan.json`、`audit/resource_events.jsonl` 和 `audit/writer_dispatch.json`。单个 writer 的 Codex 超时只累计模型推理和代码修改时间；等待资源和受控 full 运行由各自的有限超时管理，不会重复占用 Codex 活跃时间预算。

旧 LLM analysis 兼容路径必须显式开启：

```bash
python -m geng_agent review paper.pdf --out case_001 --analysis-backend llm
```

并配置 OpenAI-compatible API：

```bash
set GENG_LLM_API_KEY=...
set GENG_LLM_BASE_URL=https://api.openai.com/v1
set GENG_LLM_MODEL=...
```

## CLI 使用

只生成审查包和复现项目，不运行复现实验：

```bash
python -m geng_agent review paper.pdf --out case_001
```

运行完整复现流程：

```bash
python -m geng_agent review paper.pdf --out case_001 --run-repro
```

常用参数：

```text
--analysis-backend codex     前两阶段 backend，默认 codex；llm 为旧兼容路径
--facts-gap-rounds 6         事实抽取查漏补缺最多 6 轮，连续两轮无新增提前停止
--tasks-gap-rounds 6         任务拆解查漏补缺最多 6 轮
--codex-analysis-timeout 600 前两阶段单个 Codex 子进程超时
--codex-agent-rounds 5       每个任务 writer 最大自我修正轮数
--codex-agent-timeout 1800   单个任务 writer 子进程超时
--run-timeout 120            单次任务运行超时
--no-result-review           关闭结果对比报告生成
--no-resume                  不复用已有阶段产物，从头运行
```

检查已有 case 的阶段状态：

```bash
python -m geng_agent status case_001
```

离线汇总多个 case：

```bash
python -m geng_agent benchmark case_001 case_002 --out benchmark_report
```

## Web UI

安装 web extra 后启动：

```bash
geng-agent-web
```

浏览器打开：

```text
http://127.0.0.1:8765
```

Web UI 支持上传 PDF 或填写 PDF 链接，后台启动全流程，并通过接口实时展示阶段进度。默认 case 根目录由配置决定，可用 `GENG_CASES_ROOT` 覆盖。

## 输出目录

一次运行会生成类似结构：

```text
case_001/
  paper_chunks.json
  paper_memory.json
  memory_manifest.json
  engineering_facts.json
  fact_conflicts.json
  repro_tasks.json
  task_conflicts.json
  experiment_index.json
  paper_thesis.json
  repro_project_manifest.json
  runtime_result.json
  failure_memory.jsonl
  revision_requests.json
  automation_provenance.json
  risk_report.json
  review.md
  review.docx
  result_review.md
  result_review.docx
  audit/
    01_*.md/json/txt
    02_*.md/json/txt
    03c_task_writer_sandboxes/
    03c_task_writers_*.json
  repro_project/
    README.md
    requirements.txt
    tasks_manifest.json
    configs/
    contracts/
    tasks/
    outputs/
```

其中：

- `review.md/docx`：主报告，概述事实、任务、运行、风险和结果审查状态。
- `result_review.md/docx`：面向人工阅读的逐任务复现对比报告，包含本地复现图、论文图/子图、writer 自审结论、关键差异和证据文件。
- `runtime_result.json`：主持人结构验收、依赖、安全扫描、任务运行和产物统计。
- `automation_provenance.json`：记忆快照、分析收敛、冲突、任务契约、运行证据和修订回流的哈希化来源链。
- `risk_report.json`：可复现性风险、缺失信息、前两阶段兜底、运行异常和审计摘要。
- `audit/`：Codex prompt、stdout/stderr、JSON 校验、运行日志、图片证据等完整审计链。

## 第三阶段任务级 writer 契约

每个任务 writer 只拥有自己的 sandbox，允许修改本任务代码、私有配置、README 和 requirements。它必须：

1. 审核并完成本任务 `task_contract.json`，包括 backend 和 CPU/RAM/GPU/显存资源申请。
2. 生成符合契约的本任务复现代码和配置。
3. 在 `--run-repro` 开启时，通过 guard 校验契约并运行自己的 full。
4. 对照论文证据自审结果。
5. 不匹配时继续修改、重跑和再审查，最多 5 轮。
6. 为目标论文图/子图生成 crop 或红框 locator，并记录到 `paper_target_figure.json`。
7. 输出 `task_agent_result.json` 和 `task_agent_result.md`；只有上游分析/契约错误才输出 `task_revision_request.json`。

主持人不会重复跑全项目 full，也不会另起独立 reviewer；主持人只验收结构、合并产物、生成报告。

## 安全边界

- 生成代码、论文文本、日志、stdout/stderr 和图片内容都按不可信输入处理。
- 依赖必须通过 allowlist 和 reconciliation。
- 静态扫描会检查高风险文件操作、系统命令、网络行为等。
- Python guard 限制 writer 只能运行被分配任务，拒绝 dispatcher full 和其他任务模块。
- full 资源状态只由主持人进程内的 broker 持有；writer 只能通过自己 sandbox 内的认证通道申请租约，实际任务子进程不会继承 broker 凭据。
- 受信任 guard 使用 CPU affinity、进程树 RSS 监控、Windows Job Object、GPU 可见性/显存监控和 PyTorch 显存比例共同执行资源上限；超限会终止进程树并写入可信运行记录。

## 项目定位

耿同学 agent 的目标是提供“复现证据”和“差异解释”，不是替代人工科研判断。报告中的 `matched`、`explained_gap`、`failed` 是基于当前证据、代码和运行条件的复现状态，不等同于论文真伪结论。
