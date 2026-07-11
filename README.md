# 耿同学 agent

面向通信论文的自动复现与可信审查工具。它不是“论文真伪裁判”，而是把一篇通信论文拆成可追溯事实、可运行任务、任务级复现实验、图像证据对比和人工可读报告，帮助研究者更快判断复现结果是否支持论文结论，以及差异可能来自哪里。

当前项目生成与复现主线只使用 Codex CLI；OpenAI-compatible LLM 仅保留为前两阶段论文分析的显式兼容选项。

## 当前能力

- 解析 PDF/TXT/Markdown 论文，构建带稳定实体 ID、子图、公式、表格、章节和交叉引用的 `paper_memory.json`。
- 由一个 Codex 事实专家抽取工程事实，并把每轮合并结果作为下一轮输入，持续迭代到首轮没有语义新增。
- 无条件抽取论文核心主张、作用机制、方法排序和限制，生成 `paper_thesis.json` 作为后续复现锚点。
- 将论文图表拆成可运行的复现实验任务；`experiment_index.json` 记录目标实体、子图、参数、baseline、验收标准和 `native_full/scaled_full/proxy_only/blocked` 可复现性模式。
- 第三阶段采用任务级自治 writer：一个任务一个 Codex writer，任务数就是启动并发数；writer 写完代码后立即申请本任务 full 资源并开始运行。
- 不再启动独立 reviewer；每个 writer 自己持续修改、运行和对照论文，直到给出 `matched`、有证据的 `explained_gap` 或真实 `failed`，没有迭代轮数上限。
- 主持人不做 JSON、BOM、路径、字段、合同哈希或产物格式验收，也不二次唤醒 writer；只收集 writer 的最终科学结果。
- writer 全部结束后启动一个专用 Codex reporter，统一定位/裁切论文原图、组织语言和排版。
- 固定生成主审查报告 `review.md/docx`、本地复现报告 `reproduction_report.md/docx` 和论文对比报告 `result_review.md/docx`；对比报告不附带 writer 迭代流水账。
- 提供极简 Web UI，可上传 PDF 或填写 PDF 链接并实时查看阶段进度。

## 工作流

```text
论文 PDF
  -> 文本、页面图像、图表位置解析
  -> 构建 paper_memory.json 与 memory_manifest.json
  -> 单个 Codex 事实专家迭代抽取 engineering_facts.json
  -> 每轮读取上一轮合并产物，语义合并、去重，首轮无新增即停止
  -> Codex analysis 抽取 paper_thesis.json
  -> 单个 Codex 任务设计专家迭代生成 repro_tasks.json
  -> 同图、同指标、可共享仿真的曲线与 baseline 合并为一个任务，首轮无新增即停止
  -> experiment_index v2 可复现性分级
  -> 为每个复现任务创建独立 sandbox
  -> 每个 writer 先完成 task_contract，再写代码、运行本任务 full 并自审对比论文
  -> Python guard 限制 writer 只能运行自己的任务，校验 contract 并记录可信运行日志
  -> 所有任务 writer 同时启动；各自写完即进入 full 资源队列
  -> 主持人收集任务代码、本地图、CSV/summary 和最终科学结论
  -> 单个 Codex reporter 定位并裁切每个任务的论文原图，统一组织三份报告
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

系统会为每个复现任务准备论文文本、相关页截图和事实证据；task writer 使用它们完成科学核对，但不再承担论文图片定位与报告排版。

全部 writer 完成后，专用 reporter 读取 `task_agent_result.json`、任务 contract、本地 PNG/CSV/summary 和任务相关论文页，为每个任务生成紧凑的 `report_assets/<task_id>/paper_target.png`。对于子图任务，裁切必须保留完整坐标轴、图例、曲线和子图标签，不能用整页截图替代。

reporter 在隔离工作区内运行；它失败、超时或缺少三份报告中的任何一份时会生成 `reporter_error.json`，不会静默回退到旧的主持人拼接报告。

## Codex 配置

默认全流程走 Codex CLI：

```bash
set GENG_CODEX_CMD=codex
```

也可以按阶段覆盖：

```bash
set GENG_CODEX_ANALYSIS_CMD=codex
set GENG_CODEX_TASK_WRITER_CMD=codex
set GENG_CODEX_REPORTER_CMD=codex
```

项目启动的 Codex 子智能体默认使用 `gpt-5.5`，不跟随桌面 Codex 的全局默认模型。需要临时覆盖时可设置：

```bash
set GENG_CODEX_MODEL=gpt-5.6-luna
```

analysis 子智能体默认使用 `high` 推理强度，task writer 默认使用 `medium`，避免继承桌面配置中的 `xhigh` 后出现不必要的超长推理。可分别覆盖：

```bash
set GENG_CODEX_ANALYSIS_REASONING_EFFORT=high
set GENG_CODEX_TASK_WRITER_REASONING_EFFORT=medium
set GENG_CODEX_REPORTER_REASONING_EFFORT=high
```

task writer 采用全任务并发：有多少复现任务就同时启动多少个 writer。writer 推理并发与本地 full 并发彼此独立；任何 writer 写完代码后立刻向资源 broker 申请 full，CPU/GPU full 仍按硬件预算排队。常用运行资源上限：

```bash
set GENG_TASK_WRITER_GPU_FULL_SLOTS=1
set GENG_TASK_WRITER_CPU_FULL_SLOTS=2
set GENG_RESOURCE_RAM_RESERVE_GB=4
```

每次运行都会重新探测 CPU、可用内存、GPU、显存和 Torch/CUDA，并写入 `audit/hardware_snapshot.json`、`audit/resource_plan.json`、`audit/resource_events.jsonl` 和 `audit/writer_dispatch.json`。硬件信息只限制 full 资源租约，不减少 writer 启动数量。单个 writer 的 Codex 超时仍用于处理断网、挂死或外部阻塞；它不是科学迭代轮数上限。

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
    contracts/
    tasks/
    outputs/
```

其中：

- `review.md/docx`：主报告，概述事实、任务、运行、风险和结果审查状态。
- `reproduction_report.md/docx`：逐任务记录本地代码实际采用的关键参数、随机种子、后端、统计设置和显式假设。
- `result_review.md/docx`：逐任务并排展示本地复现图和 reporter 裁切的论文原图，并给出最终差异、原因与不确定性；不包含 writer 自我迭代附录。
- `runtime_result.json`：writer 自报完成状态、任务运行记录和产物汇总；不代表主持人格式验收。
- `automation_provenance.json`：记忆快照、分析收敛、冲突、任务契约和运行证据的哈希化来源链。
- `risk_report.json`：可复现性风险、缺失信息、前两阶段兜底、运行异常和审计摘要。
- `audit/`：Codex prompt、stdout/stderr、JSON 校验、运行日志、图片证据等完整审计链。

## 第三阶段任务级 writer 契约

每个任务 writer 只拥有自己的 sandbox，允许修改本任务代码、私有配置、README 和 requirements。它必须：

1. 审核并完成本任务 `task_contract.json`，包括 backend 和 CPU/RAM/GPU/显存资源申请。
2. 生成符合契约的本任务复现代码和配置。
3. 在 `--run-repro` 开启时，通过 guard 校验契约并运行自己的 full。
4. 对照论文证据自审结果。
5. 不匹配时继续提出新假设、修改、重跑和再审查；没有轮数上限，禁止在代码/配置未变化时机械重复 full。
6. 输出 `task_agent_result.json` 和审计用 `task_agent_result.md`；只声明本地结果图片，不生成论文 crop。

主持人不会重复跑全项目 full，也不会另起独立 reviewer或执行格式 repair。最终单独启动一个 reporter Codex，负责论文图定位、裁切和三份人工报告；Word 层只做确定性渲染，不改写报告内容。

## 安全边界

- 生成代码、论文文本、日志、stdout/stderr 和图片内容都按不可信输入处理。
- 依赖必须通过 allowlist 和 reconciliation。
- 静态扫描会检查高风险文件操作、系统命令、网络行为等。
- Python guard 限制 writer 只能运行被分配任务，拒绝 dispatcher full 和其他任务模块。
- full 资源状态只由主持人进程内的 broker 持有；writer 只能通过自己 sandbox 内的认证通道申请租约，实际任务子进程不会继承 broker 凭据。
- 受信任 guard 使用 CPU affinity、进程树 RSS 监控、Windows Job Object、GPU 可见性/显存监控和 PyTorch 显存比例共同执行资源上限；超限会终止进程树并写入可信运行记录。

## 项目定位

耿同学 agent 的目标是提供“复现证据”和“差异解释”，不是替代人工科研判断。报告中的 `matched`、`explained_gap`、`failed` 是基于当前证据、代码和运行条件的复现状态，不等同于论文真伪结论。
