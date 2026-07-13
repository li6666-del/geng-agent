# 耿同学 agent

面向通信论文的自动复现与可信审查工具。它不是“论文真伪裁判”，而是把一篇通信论文拆成可追溯事实、可运行任务、任务级复现实验、图像证据对比和人工可读报告，帮助研究者更快判断复现结果是否支持论文结论，以及差异可能来自哪里。

当前项目生成与复现主线只使用 Codex CLI；OpenAI-compatible LLM 仅保留为前两阶段论文分析的显式兼容选项。

## 当前能力

- 解析 PDF/TXT/Markdown 论文，构建带稳定实体 ID、子图、公式、表格、章节和交叉引用的 `paper_memory.json`。
- 由一个 Codex 事实专家先建立高召回实验地图；初步任务再提出中高影响 `missing_fact_requests`，程序跨任务合并去重后只进行一次定向事实回补。
- 无条件抽取论文核心主张、作用机制、方法排序和限制，生成 `paper_thesis.json` 作为后续复现锚点。
- 将论文图表拆成可运行的复现实验任务；任务描述用于导航，最终科学目标始终以论文原文和原图为准。
- 第三阶段采用任务级自治 writer：一个任务一个 Codex writer，任务数就是启动并发数；writer 自主写代码、直接运行 full、对比并迭代。
- 每个 writer 直接对照完整论文持续修改、运行和检查，完成 full 后提交 `ready_for_review`，无权授予最终 `matched`。
- 主持人只做任务覆盖、证据路径和基础代码健康检查，不参与科学结论。
- 每个 writer 交付后立即启动对应的独立 Codex task reporter，直接核对论文与本地产物；不通过的任务单独回到原 sandbox，已通过任务不重跑。全部通过后才启动 Final Report Editor。
- 固定生成主审查报告 `review.md/docx`、本地复现报告 `reproduction_report.md/docx` 和论文对比报告 `result_review.md/docx`；对比报告不附带 writer 迭代流水账。
- 提供极简 Web UI，可上传 PDF 或填写 PDF 链接并实时查看阶段进度。

## 工作流

```text
论文 PDF
  -> 文本、页面图像、图表位置解析
  -> 构建 paper_memory.json 与 memory_manifest.json
  -> 单个 Codex 事实专家生成 engineering_facts_initial.json，建立高召回实验地图
  -> 图中近似读数、视觉结构、附录公式和冲突观察均带来源/置信度保留，不做语义删除
  -> 单个 Codex 任务专家生成 repro_tasks_preliminary.json，并提出结构化事实缺口
  -> 程序按 type + name 跨任务合并去重，仅对中高影响缺口执行一次定向事实回补
  -> 合并生成 engineering_facts.json；未找到的证据显式保留为 missing_information
  -> 任务专家根据回补结果定稿 repro_tasks.json
  -> Codex analysis 基于最终事实抽取 paper_thesis.json
  -> experiment_index v2 记录任务、图表、参数、baseline 和证据定位，不做运行前复现评级
  -> 为每个复现任务创建独立 sandbox
  -> 所有任务 writer 同时启动，在独立 sandbox 内自主写代码、选择硬件并直接运行 full
  -> 每个 writer 自行对比论文、修改代码并反复运行，完成可靠 full 后提交 ready_for_review
  -> 每个任务完成后立刻启动独立 task reporter，直接对照论文并定位裁图；差异只反馈给对应 writer
  -> 全部任务通过后系统授予 matched，Final Report Editor 统一组织三份报告
  -> 输出 review、reproduction_report、result_review 的 Markdown/Word 版本
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

系统会把原始论文文件、全论文页面图以及前两轮最终定稿的 `engineering_facts.json`、`repro_tasks.json`、`experiment_index.json` 和可选 `paper_thesis.json` 复制到每个 writer sandbox。所有论文页面图直接随 Codex writer 会话发送，不再执行任务页筛选；任务相关事实摘要只用于文本导航，不构成信息边界。

每个 writer 交付后，专属 task reporter 在独立上下文中读取该任务的 `task_agent_result.json`、执行摘要、本地 PNG/CSV/summary 和完整论文证据，直接判断 `accepted/revise` 并定位论文原图。科学差异只退回对应 writer；裁图或证据定位问题只重跑对应 task reporter。所有任务通过后，Final Report Editor 只读取已验收的紧凑任务包和图片，汇总生成三份最终报告。

每个 task reporter 都拥有独立工作区，绝不接收其他实验的本地产物或结论；最终编辑器没有科学裁决权。任务级或编辑器级进程失败会分别记录在对应 audit 状态中。

## Codex 配置

默认全流程走 Codex CLI：

```bash
set GENG_CODEX_CMD=codex
```

也可以按阶段覆盖：

```bash
set GENG_CODEX_ANALYSIS_CMD=codex
set GENG_CODEX_TASK_WRITER_CMD=codex
set GENG_CODEX_TASK_REPORTER_CMD=codex
set GENG_CODEX_REPORT_EDITOR_CMD=codex
```

项目启动的 Codex 子智能体默认使用 `gpt-5.5`，不跟随桌面 Codex 的全局默认模型。需要临时覆盖时可设置：

```bash
set GENG_CODEX_MODEL=gpt-5.6-luna
```

analysis 与 task reporter 默认使用 `high` 推理强度，task writer 与 Final Report Editor 默认使用 `medium`，避免继承桌面配置中的 `xhigh` 后出现不必要的超长推理。可分别覆盖：

```bash
set GENG_CODEX_ANALYSIS_REASONING_EFFORT=high
set GENG_CODEX_TASK_WRITER_REASONING_EFFORT=medium
set GENG_CODEX_TASK_REPORTER_REASONING_EFFORT=high
set GENG_CODEX_REPORT_EDITOR_REASONING_EFFORT=medium
```

task writer 采用全任务并发：有多少复现任务就同时启动多少个 writer。每个 writer 在自己的 sandbox 内直接调用当前 Python，自行探测 CPU/GPU、选择 backend、声明依赖并运行 smoke/full；主持人不做资源排队、命令拦截、科学迭代超时或中途重试。

Writer 必须先从抽取事实、原论文 PDF、caption、正文、公式、表格和附录中补找缺失参数；仍找不到时才做可追踪的科学假设，并把假设当作后续图像对比中的可调变量。对 Monte Carlo、批量矩阵运算和分钟级 CPU full，CUDA 可用时应优先实现真实 Torch CUDA 计算路径；仅调用 backend selector 或在报告中写 GPU 名称不算使用 GPU。

Writer 使用直接的对比迭代：运行 full，逐项对照论文原文和原图，先写出具体修改方针，再修改代码、参数或配置并重新运行。修改后再次比较同一组差异，决定保留、回退或继续调整；不设置固定轮数，也不为凑次数运行无变化的 full。Writer 只能提交带完整运行与图像证据的 `ready_for_review`；最终 `matched` 由 Reporter 直接核验论文后授予。

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

只运行任务驱动的前两阶段，不启动任何 writer 或 reporter：

```bash
python -m geng_agent review paper.pdf --out case_analysis --analysis-only
```

常用参数：

```text
--analysis-backend codex     前两阶段 backend，默认 codex；llm 为旧兼容路径
--analysis-only              只生成最终事实、最终任务、论文主张和实验索引
--codex-analysis-timeout 600 前两阶段单个 Codex 子进程超时
--codex-agent-timeout 1800   单个任务 writer 子进程超时
--codex-reporter-timeout 1800 最终报告 Codex 子进程超时
--run-timeout 120            单次任务运行超时
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
  engineering_facts_initial.json
  repro_tasks_preliminary.json
  engineering_facts_backfill.json
  engineering_facts.json
  fact_conflicts.json
  repro_tasks.json
  task_conflicts.json
  experiment_index.json
  paper_thesis.json
  repro_project_manifest.json
  runtime_result.json
  report_assets/
  reproduction_report.md
  reproduction_report.docx
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
    04_reporter_*.json/md/txt
    04_reporter_workspace/
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
- `reproduction_report.md/docx`：逐任务记录本地代码实际采用的关键参数、随机种子、后端、统计设置和显式假设。
- `result_review.md/docx`：逐任务并排展示本地复现图和 reporter 裁切的论文原图，并给出最终差异、原因与不确定性；不包含 writer 自我迭代附录。
- `verification_result.json`：Reporter 对论文证据与本地结果的逐任务直接裁决。
- `runtime_result.json`：执行摘要、产物汇总和最终独立验收状态；候选阶段不计为最终 matched。
- `automation_provenance.json`：记忆快照、初始事实、初步任务、定向回补、冲突和 writer 执行证据的哈希化来源链。
- `risk_report.json`：可复现性风险、缺失信息、前两阶段兜底、运行异常和审计摘要。
- `audit/`：Codex prompt、stdout/stderr、JSON 校验、运行日志、图片证据等完整审计链。

## 第三阶段任务级 writer 自治循环

每个任务 writer 只拥有自己的 sandbox，允许修改本任务代码、私有配置、README 和 requirements。它必须：

1. 阅读任务事实、论文证据和完整目标图，生成本任务复现代码与配置。
2. 自行探测硬件并选择 CPU/GPU、并行度、批量大小和依赖。
3. 在 `--run-repro` 开启时直接运行自己的 smoke/full。
4. 逐项对照论文图的曲线、baseline、数值形状、坐标轴、单位、尺度、统计量、标注、图例和样式。
5. 不匹配时先回查论文参数，再提出显式假设、修改、重跑和再审查；没有轮数上限，禁止在代码/配置未变化时机械重复 full。
6. 完成成功 full 并直接对照完整论文后输出 `ready_for_review`，提供本地图、CSV/summary、参数来源和差异说明；Writer 不能输出最终 matched。

主持人同时启动所有 writers。每个 Writer 交付后，只有其对应的 task reporter 会立刻在独立上下文中审查该任务；科学差异仅让该 Writer 在原 sandbox 继续修改和 full，其他任务不受影响。所有任务通过后，Final Report Editor 只读取已验收任务包与图片，负责三份人工报告的语言组织与排版。

## 安全边界

- 生成代码、论文文本、日志、stdout/stderr 和图片内容都按不可信输入处理。
- 依赖必须通过 allowlist 和 reconciliation。
- 静态扫描会检查高风险文件操作、系统命令、网络行为等。
- 每个 writer 使用独立 sandbox 隔离任务文件；运行权限和资源决策由 writer 自己负责。
- 主持人会确定性清理 Python BOM；静态扫描发现语法错误时 runtime 不得显示通过。
- `matched` 要求 Reporter 直接检查论文目标与本地产物后确认无实质差异；仅定性趋势、排序或大致外观一致不能通过。

## 项目定位

耿同学 agent 的目标是提供尽可能逼近原论文的复现证据，不是替代人工科研判断。`matched` 只表示可观察目标经独立 task reporter 直接核验通过，不表示恢复了作者未公开代码；外部 Codex、网络、额度或运行环境错误会保留为未完成状态。
