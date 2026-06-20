# 耿同学agent

耿同学agent 是一个本地运行的通信领域论文工程复现审查 CLI。它不直接判定论文真假，而是把论文转换成可追溯的工程事实、复现任务、复现项目、运行结果和复现风险报告。

## 快速开始（新用户）

在自己的机器上从零跑通（Windows / PowerShell；用你自己的 Python；本机启动器默认使用 `%USERPROFILE%\miniconda3\envs\torch\python.exe`）：

```powershell
# 1. 拉代码
git clone https://github.com/li6666-del/geng-agent.git
cd geng-agent

# 2. 一条命令装齐（运行本体 + 复现白名单库）
python -m pip install -e ".[repro]"

# 3. 喂 PDF 前先自检环境，全绿再继续
python -m geng_agent doctor

# 4. 确认 Codex CLI 可用（默认全流程使用 Codex 子进程）
$env:GENG_CODEX_CMD="codex"
# 可选：为不同阶段指定不同 Codex 命令
# $env:GENG_CODEX_ANALYSIS_CMD="codex"
# $env:GENG_CODEX_WRITER_CMD="codex"
# $env:GENG_CODEX_REVIEWER_CMD="codex"

# 5. 拿仓库自带的示例论文跑一遍“全流程”
#    = 受限运行复现 + 结果级多模态审查（结果级审查与兜底默认开）
python -m geng_agent review sample_papers\rayleigh_error_probability_2406.16548.pdf --out case_001 --run-repro
```

跑完看 `case_001\` 下的 `review.md`（主报告）、`repro_project\`（生成的复现项目）、`result_review.md`（结果级审查）。

> 要点：① 默认路径依赖本机 Codex CLI 登录状态，不需要 `GENG_LLM_API_KEY`；② 只想生成、不运行代码就去掉 `--run-repro`；③ 想跑得更稳可调 `--codex-agent-rounds`、`--codex-agent-stall-rounds`、`--run-timeout` 等参数；④ 只有显式使用 `--analysis-backend llm` 的旧路径才需要配置 OpenAI-compatible API。安装、运行解释器、模型配置的细节见下文各节。

## 工作流

```text
论文 PDF/TXT/Markdown
  -> 本地解析成带 chunk_id/page/section 的文本块
  -> Codex analysis 子智能体抽取 engineering_facts.json
  -> 本地归一化：枚举近义词归位、补默认、修正空字段、剥多余键，改动全部记入 _meta
  -> 本地用 Pydantic schema 审查 JSON 结构和 source.chunk_id 回指
  -> 校验失败时只丢无法修复或无出处的单条事实，保留其余（部分接受）
  -> 输出被截断时从可解析前缀抢救事实数组；仅当零条可用事实才退本地关键词 fallback
  -> Codex analysis 子智能体生成 repro_tasks.json
  -> 本地审查任务引用的 fact、指标公式、输出列、趋势和 baseline
  -> Codex writer 子智能体生成/修改 repro_project/
  -> 本地审查路径、内容、依赖白名单、必要文件和语法
  -> 默认停止，不自动运行生成代码
  -> 用户显式传 --run-repro 时，进入受限运行器
  -> 运行器做依赖白名单、静态安全扫描（禁网络/子进程导入、禁危险调用、禁 eval/exec/__import__/getattr 等反射类动态执行内置函数）、干净环境变量、outputs 新鲜度和格式校验
  -> 运行结果和 reviewer 反馈进入 Codex writer/reviewer 自适应闭环
  -> 运行通过后，默认进入结果级多模态二次审查
  -> 把 CSV/summary、本地 PNG、论文页面 PNG 和论文上下文交给 Codex reviewer
  -> 生成 result_review.md、risk_report.json 和 review.md
```

## 安装与部署（输入 PDF 前必看）

换任何机器，本质只要满足两条，**与盘符无关**——本机默认的 torch 环境路径只是本机路径，别的机器换成它自己的 Python 即可：

**① Python 版本 ≥ 3.11**（开发使用 3.13）。

**② 该装的库全装**——一条命令同时装齐两类缺一不可的依赖：

```bash
# 把下面路径换成目标机器上你要用的那个 Python（在 C 盘、或已加入 PATH 时直接写 python 都行）
C:\Users\84475\miniconda3\envs\torch\python.exe -m pip install -e ".[repro]"
```

| 这条命令装的两类 | 包 | 缺了会怎样 |
|---|---|---|
| 运行 geng-agent 本体 | pypdf、pymupdf、pydantic、python-docx、pillow | CLI 起不来 |
| 复现代码白名单 | numpy、scipy、matplotlib、scikit-learn、reedsolo、pandas、sympy、numba、scikit-commpy、galois、networkx、h5py、tqdm | 复现代码跑不了 → 每篇论文吃兜底（缺 numpy/scipy/matplotlib 这三个关键库时生成阶段还会反过来让模型“别用它们”） |

> 需要极简 Web 就装 `".[repro,web]"`。从项目目录直接运行时 `geng_agent` 包本身可以不装，但上面这些库必须装。

装完，**喂 PDF 之前先自检一次**：

```powershell
.\run.ps1 doctor          # 或：C:\Users\84475\miniconda3\envs\torch\python.exe -m geng_agent doctor
```

`doctor` 回显它实际使用的解释器路径、Python 版本是否达标、每个库装没装：**全绿才喂 PDF**；缺什么它直接给出修复命令（致命缺失退出码 1、可用 0）。因为它报的是真实解释器路径，能当场看出有没有指错 Python——所以那条路径本身不用记死，让 `doctor` 替你确认。

> 白名单的唯一真源是 `security.py` 的 `ALLOWED_REQUIREMENTS`，`pyproject.toml` 的 `[repro]` 由 `tests/test_preflight.py` 锁死、不会漂移。`review` 启动时也会自动做一次轻量自检，缺关键库会在 stderr 告警（不阻断运行）。

## 运行解释器（重要）

复现项目（`run_experiment.py`）需要 numpy/scipy/matplotlib。本机默认的 `python`（harness 自带 venv）**没有**这些包，用它跑会让每一篇用到 numpy 的论文都退本地模板兜底。

本项目固定使用一个完整解释器：**`C:\Users\84475\miniconda3\envs\torch\python.exe`**（Python 3.11，已装 numpy/scipy/matplotlib/torch/CUDA 及全部依赖）。直接用仓库里的启动器，它会用正确的解释器调用 geng-agent，并自动切到项目目录：

```powershell
# PowerShell
.\run.ps1 review paper.pdf --out case_001 --run-repro
# .ps1 被执行策略拦截时，用 cmd 启动器或 bypass：
.\run.cmd review paper.pdf --out case_001 --run-repro
powershell -ExecutionPolicy Bypass -File run.ps1 review paper.pdf --out case_001 --run-repro
```

也可以直接显式调用解释器：

```powershell
C:\Users\84475\miniconda3\envs\torch\python.exe -m geng_agent review paper.pdf --out case_001 --run-repro
```

> 解释器路径变了，可以设置 `GENG_PYTHON` 临时覆盖，或改 `run.ps1` / `run.cmd` 顶部的默认路径。下文示例里的 `python` 一律指这个解释器（建议用 `.\run.ps1` 代替 `python -m geng_agent`）。

## 配置 Codex / 兼容模型

### 默认：Codex analysis + writer/reviewer

```powershell
$env:GENG_CODEX_CMD="codex"              # 默认命令
$env:GENG_CODEX_ANALYSIS_CMD="codex"     # 可选：单独指定前两阶段分析子智能体
$env:GENG_CODEX_WRITER_CMD="codex"       # 可选：单独指定写代码子智能体
$env:GENG_CODEX_REVIEWER_CMD="codex"     # 可选：单独指定结果审查子智能体
```

前两阶段包括 `engineering_facts.json`、facts gap、可选 `paper_thesis.json`、`repro_tasks.json` 和 tasks gap。Codex 子智能体只负责产出候选 JSON；最终能否进入下一阶段仍由本地 schema、source 校验、coverage gap、任务引用校验和 fallback 策略决定。

### 旧 LLM analysis 兼容路径

需要显式加 `--analysis-backend llm` 才会使用 OpenAI 兼容 API：

```powershell
$env:GENG_LLM_API_KEY="你的 API Key"
$env:GENG_LLM_BASE_URL="https://api.openai.com/v1"
$env:GENG_LLM_MODEL="你的模型名"
```

### 第二事实抽取模型（仅旧 LLM analysis 可选）

`--analysis-backend llm` 时，第一轮事实抽取支持第二个多模态模型做 ensemble，减少图表和公式漏抽。三项都设置时启用；缺任意一项则保持单模型行为。默认 Codex analysis 路径不会叠加 `GENG_LLM2_*`。

```powershell
$env:GENG_LLM2_MODEL="你的第二模型名"
$env:GENG_LLM2_BASE_URL="https://api.example.com/v1"
$env:GENG_LLM2_API_KEY="你的第二模型 API Key"
```

## 极简 Web（实时阶段进度）

仅需上传 PDF 或填写 PDF 链接，其余全自动（含运行复现与结果审查）。页面只显示阶段进度，无多余功能。

```powershell
pip install -e ".[web]"
$env:GENG_CODEX_CMD="codex"
# 可选：案例输出目录，默认 %USERPROFILE%\Documents\geng_cases
# $env:GENG_CASES_ROOT="C:\Users\84475\Documents\耿同学agent"

geng-agent-web
# 浏览器打开 http://127.0.0.1:8765
```

## 使用

默认只生成审查包和复现项目，不自动运行生成代码：

```bash
python -m geng_agent review paper.pdf --out case_001
```

显式运行受限复现器；运行通过后会自动执行结果级多模态二次审查：

```bash
python -m geng_agent review paper.pdf --out case_001 --run-repro
```

如果只想运行复现，不想执行结果级多模态审查：

```bash
python -m geng_agent review paper.pdf --out case_001 --run-repro --no-result-review
```

默认模式下，前两阶段 analysis 和第三阶段 project generation 都走 Codex CLI 子进程：

```bash
python -m geng_agent review paper.pdf --out case_001 --run-repro
```

Codex analysis 子进程负责事实抽取和任务拆解；Codex writer/reviewer 子进程负责生成代码、运行反馈和论文-结果对比。旧 LLM analysis 只在显式传 `--analysis-backend llm` 时启用。

Optional Codex controls:

```bash
python -m geng_agent review paper.pdf --out case_001 --run-repro --codex-analysis-timeout 600 --codex-agent-rounds 8 --codex-agent-stall-rounds 2 --codex-agent-timeout 1800
set GENG_CODEX_CMD=codex
set GENG_CODEX_ANALYSIS_CMD=codex
set GENG_CODEX_WRITER_CMD=codex
set GENG_CODEX_REVIEWER_CMD=codex
```

常用参数：

```text
--analysis-backend codex    前两阶段事实抽取/任务拆解 backend；默认 codex，llm 为旧兼容路径
--codex-analysis-timeout 600 前两阶段单个 Codex analysis 子进程超时
--facts-gap-rounds 6         第一轮事实查漏补缺最多 6 轮；一轮无新增即停，设 0 关闭
--tasks-gap-rounds 6         第二轮任务查漏补缺最多 6 轮；全覆盖或一轮无新增即停，设 0 关闭
--codex-agent-rounds 8       第三轮 Codex writer/reviewer 自适应闭环最大轮数；默认 8，不代表固定跑满
--codex-agent-stall-rounds 2 连续 2 轮没有刷新最佳评分后停止；设 0 可关闭平台期停止
--codex-agent-timeout 1800   单个 Codex writer/reviewer 子进程超时；当前没有第三轮总墙钟预算
--json-repair-attempts 3   每轮 JSON 审查失败后的返修次数
--run-repro                显式运行生成的复现项目
--no-result-review         关闭运行成功后的结果级多模态二次审查
--repair-attempts 2        复现代码运行失败后的自动修复次数
--run-timeout 120          单次复现运行超时时间，单位秒
--no-run-repro             不自动运行生成代码；默认就是这个行为
```

## 输出目录

```text
case_001/
  paper_chunks.json
  engineering_facts.json
  repro_tasks.json
  risk_report.json
  generated_files.json
  review.md
  result_review.md
  result_review.json          # 旧结构化 result-review 路径可能生成
  result_review_error.json
  audit/
    01_extract_engineering_facts.md
    02_build_repro_tasks.md
    03c_agentic_project_*.json
    03c_*_brief.md
    raw_*.txt
    validation_*.json
  repro_project/
    README.md
    requirements.txt
    config.json
    config_smoke.json
    run_experiment.py
    repair_logs/
    src/
      channel.py
      modulation.py
      metrics.py
      simulation.py
    outputs/
```

默认 Codex 路径下，`result_review.md` 只会在 `--run-repro` 产生可用输出并且 reviewer 完成时生成；失败时生成 `result_review_error.json`。`result_review.json` 主要保留给旧结构化 result-review 路径。

## Schema 真源

结构规则由 Pydantic models 统一定义：

```text
geng_agent/schema_models.py
```

这些 models 会导出正式 JSON Schema：

```text
schemas/
  engineering_facts.schema.json
  repro_tasks.schema.json
  repro_project_manifest.schema.json
  repair_manifest.schema.json
  result_review.schema.json
```

重新导出命令：

```bash
python -m geng_agent.export_schemas --out schemas
```

默认 Codex analysis 阶段会把 JSON Schema 写入 audit 并在 prompt 中展示，但不把含自由字典字段的 schema 直接传给 Codex CLI 的 `--output-schema`；最终结构、source 回指和业务规则由本地 Pydantic/coverage 校验兜底。旧 `--analysis-backend llm` 路径会把 JSON Schema 作为 `response_format` 发给兼容模型，如果服务端不支持严格 `json_schema`，客户端会退回 `json_object`。无论哪条路径，本地都会用 Pydantic 再审查一次。

## 核心原则

- 默认由 Codex 子智能体负责抽取事实、设计复现任务、生成代码和审查复现结果；旧 LLM analysis 只在显式选择时启用。
- 本地程序负责 Pydantic JSON 审查、路径审查、安全扫描、语法检查、运行验证、图像打包和风险汇总。
- 首轮工程事实先做本地归一化、部分接受和截断抢救，尽量保住模型的真实抽取而不是退回关键词 fallback；所有纠正和被丢弃的事实都记入 `_meta` 并在 `risk_report.json` 标注，绝不凭空编造，无有效 `source.chunk_id` 出处的事实仍会被丢弃。
- 论文文本、日志、stdout/stderr、代码片段、表格和图像都按 `UNTRUSTED DATA` 处理。
- `risk_report.json` 和 `result_review.md` 只表达复现风险与差异分析，不直接给出造假结论。
- smoke 通过只说明“轻量复现项目能跑并生成格式正确的产物”，不代表论文已经完整复现。
