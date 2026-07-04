# 耿同学 agent

面向通信论文的自动复现与可信审查工具。它不是“论文真伪裁判”，而是把一篇通信论文拆成可追溯事实、可运行任务、任务级复现实验、图像证据对比和人工可读报告，帮助研究者更快判断复现结果是否支持论文结论，以及差异可能来自哪里。

当前主线默认使用 Codex CLI 子进程完成论文理解和任务级复现；旧 OpenAI-compatible LLM 路径仍保留为显式兼容选项。

## 当前能力

- 解析 PDF/TXT/Markdown 论文，保留文本块、页码、页面图像和图表证据。
- 由 Codex analysis 子智能体抽取工程事实，并通过本地 schema、来源回指和多轮 gap check 校验。
- 将论文图表拆成可运行的复现实验任务，记录输入事实、预期产物、风险点和论文证据。
- 第三阶段采用任务级自治 writer：一个任务一个 Codex writer，独立写代码；传 `--run-repro` 时通过 guard 跑本任务 full、对照论文图、自我修正，最多 5 轮。
- 不再启动独立 reviewer；每个 writer 自己给出 `matched`、`explained_gap` 或 `failed`，主持人只做结构验收、依赖/安全检查、产物合并和报告生成。
- 生成主报告 `review.md/docx`、结果对比报告 `result_review.md/docx`、运行结果、风险报告和完整 audit 证据链。
- 提供极简 Web UI，可上传 PDF 或填写 PDF 链接并实时查看阶段进度。

## 工作流

```text
论文 PDF
  -> 文本、页面图像、图表位置解析
  -> Codex analysis 抽取 engineering_facts.json
  -> 本地事实归一化、source 回指校验、缺失事实 gap check
  -> Codex analysis 生成 repro_tasks.json
  -> 本地任务引用、指标、趋势、baseline 与覆盖率校验
  -> 为每个复现任务创建独立 sandbox
  -> 每个 Codex task writer 写本任务代码、声明依赖；--run-repro 时运行本任务 full 并自审对比论文
  -> Python guard 限制 writer 只能运行自己的任务，并记录可信运行日志
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

`doctor` 会检查 Python 版本、运行本体依赖、复现白名单库，以及可选的论文图表抽取工具。缺关键库时请先修复，再喂论文。

## 论文原图抽取

`result_review.md/docx` 的论文原图优先使用 PDFFigures2 抽取出的 Figure/Table 边界。配置方式：

```bash
set GENG_PDFFIGURES2_CMD=C:\tools\pdffigures2.jar
```

如果默认 `java` 不是可用的 JDK，也可以额外设置：

```bash
set GENG_PDFFIGURES2_JAVA_CMD=C:\tools\jdk17\bin\java.exe
```

也可以设置为完整命令模板，例如包含 `{pdf}`、`{json_dir}`、`{image_prefix}`、`{stats}` 的启动命令。模板会以参数列表执行，不经过 shell。没有配置或运行失败时，流程不会中断，但结果报告会回退到整页论文截图并标注低置信度。

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
--facts-gap-rounds 10        事实抽取查漏补缺最多 10 轮
--tasks-gap-rounds 6         任务拆解查漏补缺最多 6 轮
--science-loop               启用论文思路锚点，供任务 writer 使用
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
  engineering_facts.json
  repro_tasks.json
  experiment_index.json
  paper_thesis.json
  repro_project_manifest.json
  runtime_result.json
  risk_report.json
  review.md
  review.docx
  result_review.md
  result_review.docx
  audit/
    01_*.md/json/txt
    02_*.md/json/txt
    03c_task_writer_sandboxes/
    03c_reviewer_images/
    03c_task_writers_*.json
  repro_project/
    README.md
    requirements.txt
    tasks_manifest.json
    configs/
    tasks/
    outputs/
```

其中：

- `review.md/docx`：主报告，概述事实、任务、运行、风险和结果审查状态。
- `result_review.md/docx`：面向人工阅读的逐任务复现对比报告，包含本地复现图、论文图/子图、writer 自审结论、关键差异和证据文件。
- `runtime_result.json`：主持人结构验收、依赖、安全扫描、任务运行和产物统计。
- `risk_report.json`：可复现性风险、缺失信息、fallback、运行异常和审计摘要。
- `audit/`：Codex prompt、stdout/stderr、JSON 校验、运行日志、图片证据等完整审计链。

## 第三阶段任务级 writer 契约

每个任务 writer 只拥有自己的 sandbox，允许修改本任务代码、私有配置、README 和 requirements。它必须：

1. 生成本任务复现代码和配置。
2. 在 `--run-repro` 开启时，通过 guard 运行自己的 full。
3. 对照论文证据自审结果。
4. 不匹配时继续修改、重跑和再审查，最多 5 轮。
5. 输出 `task_agent_result.json` 和 `task_agent_result.md`。

主持人不会重复跑全项目 full，也不会另起独立 reviewer；主持人只验收结构、合并产物、生成报告。

## 安全边界

- 生成代码、论文文本、日志、stdout/stderr 和图片内容都按不可信输入处理。
- 依赖必须通过 allowlist 和 reconciliation。
- 静态扫描会检查高风险文件操作、系统命令、网络行为等。
- Python guard 限制 writer 只能运行被分配任务，拒绝 dispatcher full 和其他任务模块。
- full 运行有并发限流和超时，运行记录写入审计链。

## 项目定位

耿同学 agent 的目标是提供“复现证据”和“差异解释”，不是替代人工科研判断。报告中的 `matched`、`explained_gap`、`failed` 是基于当前证据、代码和运行条件的复现状态，不等同于论文真伪结论。
