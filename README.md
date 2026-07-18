# 耿同学 agent

面向通信论文的自动复现与可信审查工具。它不是“论文真伪裁判”，而是把一篇通信论文拆成可追溯事实、可运行任务、任务级复现实验、图像证据对比和人工可读报告，帮助研究者更快判断复现结果是否支持论文结论，以及差异可能来自哪里。

当前项目生成与复现主线只使用 Codex CLI；OpenAI-compatible LLM 仅保留为前两阶段论文分析的显式兼容选项。

## 当前能力

- 解析 PDF/TXT/Markdown 论文，保留全文分块、页面图像和带 caption/page/bbox 的图候选索引，供 Codex 直接查证。
- 一个 Codex 事实专家只做一轮全局事实扫描，一个任务专家只做一轮初步设计并立即输出 `backfill_handoff`；只有明确点名的实验定义 blocker 才进入定向回补，通常 0–2 轮，第三轮仅作异常熔断。
- 无条件抽取论文核心主张、作用机制、方法排序和限制，生成 `paper_thesis.json` 作为后续复现锚点。
- 将论文图表拆成可运行的复现实验任务；任务描述用于导航，最终科学目标始终以论文原文和原图为准。
- 第三阶段采用任务级自治 writer：一个任务一个 Codex writer，任务数就是启动并发数；writer 自主写代码、直接运行 full、对比并迭代。
- 每个 writer 把论文明确事实作为最高约束，只在论文未披露或确有歧义处作显式工程假设；完成 full、支持核心观点且不存在材料性事实冲突后提交 `ready_for_review`，无权授予最终 `matched`。
- 主持人只做任务覆盖、证据路径和基础代码健康检查，不参与科学结论。
- 每个 writer 交付后立即启动对应的独立 Codex task reporter，直接核对论文与本地产物；Reporter 只为违反明确事实或核心观点未得到支持的材料性问题打回任务，合理假设和非材料差异作为限制记录。已通过任务不重跑，全部通过后才启动 Final Report Editor。
- 固定生成主审查报告 `review.md/docx`、本地复现报告 `reproduction_report.md/docx` 和论文对比报告 `result_review.md/docx`；对比报告不附带 writer 迭代流水账。
- 提供极简 Web UI，可上传 PDF 或填写 PDF 链接并实时查看阶段进度。

## 工作流

```text
论文 PDF
  -> 文本与页面图像解析；MinerU 可选预解析一次，生成带 caption/page/bbox 的整图候选索引
  -> 生成 paper_chunks.json 与 paper_figure_index.json，不增加 Python 语义实体中间层
  -> 单个 Codex 事实专家生成 engineering_facts_initial.json，建立高召回实验地图
  -> 图中近似读数、视觉结构、附录公式和冲突观察均带来源/置信度保留，不做语义删除
  -> 单个 Codex 任务专家生成 repro_tasks_preliminary.json，并提出结构化事实缺口
  -> 程序按稳定请求键和 required_fields 跨任务合并去重；任务专家先选择真正阻塞 Writer 的 request_id
  -> 每轮按字段记录论文证据、未找到位置或冲突，并把搜索结果写入累计台账
  -> 没有 blocker 时直接交给 Writer；有 blocker 时只回补选中项，任务刷新后再次软交接，同一未解字段最多搜索两次
  -> Python 只硬验收 JSON/基本结构；来源、引用、证据类型和缺失字段写入 analysis_warnings.json，不触发科学内容重写
  -> Codex analysis 基于最终事实抽取 paper_thesis.json
  -> experiment_index v2 记录任务、图表、参数、baseline 和证据定位，不做运行前复现评级
  -> 为每个复现任务创建独立 sandbox
  -> 所有任务 writer 同时启动，在独立 sandbox 内自主写代码、选择硬件并直接运行 full
  -> 每个 writer 自行对比论文、修改代码并反复运行，完成可靠 full 后提交 ready_for_review
  -> 每个任务完成后立刻启动独立 task reporter，按“明确事实忠实 + 核心观点支持”的材料性门槛裁决，并在 MinerU 父图内定位目标子图
  -> Python 按 Reporter 提交且完成视觉复检的坐标从原 PDF 高分辨率裁切；边界不可靠时自动回退完整父图
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

### 可选 MinerU 图定位增强

MinerU 不安装进主项目或 torch 复现环境，建议使用独立 Conda 环境。它在 PDF 载入后、事实抽取前只运行一次，负责生成整图候选，不负责科学审查，也不直接决定最终子图边界。项目调用时关闭 MinerU 的公式和表格识别，只保留图定位所需的版面解析，避免为无关能力付出大段 CPU 时间。

```powershell
conda create -n mineru python=3.11 -y
conda run -n mineru python -m pip install -U "mineru[all]"

# 按实际安装位置设置；命令也可以是带参数的完整命令行
setx GENG_MINERU_CMD "C:\Users\<you>\miniconda3\envs\mineru\Scripts\mineru.exe"
# 可选：指定 MinerU backend；未设置时使用 MinerU 默认值
setx GENG_MINERU_BACKEND "pipeline"
# 可选：把模型缓存放到空间更充足的磁盘；只传给 MinerU 子进程
setx GENG_MINERU_CACHE_ROOT "D:\geng-tools\mineru-cache"
```

MinerU 缺失、超时、非零退出或未识别到目标图时，流程不会失败：Task Reporter 继续使用完整论文与页面图定位，状态和回退原因记录在 `audit/00_mineru/mineru_status.json`。

## 论文目标图定位

系统会把原始论文文件、全论文页面图以及前两轮最终定稿的 `engineering_facts.json`、`repro_tasks.json`、`experiment_index.json`、可选 `paper_thesis.json` 和 `analysis_warnings.json` 复制到每个 writer sandbox。所有论文页面图直接随 Codex writer 会话发送，不再执行任务页筛选；任务相关事实摘要只用于文本导航，不构成信息边界。

每个 writer 交付后，专属 task reporter 在独立上下文中读取该任务的 `task_agent_result.json`、执行摘要、本地 PNG/CSV/summary 和完整论文证据，直接判断 `accepted/revise` 并定位论文原图。科学差异只退回对应 writer；裁图或证据定位问题只重跑对应 task reporter。所有任务通过后，Final Report Editor 只读取已验收的紧凑任务包和图片，汇总生成三份最终报告。

对 PDF，系统优先把 MinerU 的整图候选连同 caption、页码和归一化 bbox 交给 task reporter。Reporter 只在候选父图内部标注目标子图，并显式复检目标身份、面板边界、坐标轴、图例和关键注释；Python 再从原 PDF 确定性裁切。任一复检项不确定时使用完整父图，避免“为了紧凑而斩断图”。

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

项目启动的 Codex 子智能体默认使用 `gpt-5.6-sol`，推理强度统一为 `xhigh`，不跟随桌面 Codex 的全局默认配置。需要临时覆盖时可设置：

```bash
set GENG_CODEX_MODEL=gpt-5.6-luna
set GENG_CODEX_REASONING_EFFORT=high
```

需要针对不同角色调整推理强度时，可分别覆盖：

```bash
set GENG_CODEX_ANALYSIS_REASONING_EFFORT=xhigh
set GENG_CODEX_TASK_WRITER_REASONING_EFFORT=xhigh
set GENG_CODEX_TASK_REPORTER_REASONING_EFFORT=xhigh
set GENG_CODEX_REPORT_EDITOR_REASONING_EFFORT=xhigh
```

task writer 采用全任务并发：有多少复现任务就同时启动多少个 writer。每个 writer 在自己的 sandbox 内直接调用当前 Python，自行探测 CPU/GPU、选择 backend、声明依赖并运行 smoke/full；主持人不做资源排队、命令拦截、科学迭代超时或中途重试。

Writer 必须先读取 `repro_tasks.json` 的 `_meta.fact_gap_handoff`，再从抽取事实、原论文 PDF、caption、正文、公式、表格和附录中补找缺失参数；前两阶段的未解决记录只是导航，不是停止依据。论文明确给出的数据、模型、公式、算法和实验协议不可为了贴图而改动；仍找不到的值或实现细节才允许成为可追踪、可调整的科学假设。对 Monte Carlo、批量矩阵运算和分钟级 CPU full，CUDA 可用时应优先实现真实 Torch CUDA 计算路径；仅调用 backend selector 或在报告中写 GPU 名称不算使用 GPU。

Writer 使用直接的对比迭代：运行 full，先核验明确事实和核心观点，再区分材料性问题、合理的论文空白假设与非材料差异。只有材料性问题且存在论文证据支撑的修改方向时才修改并重新 full；不得盲从与论文冲突的 Reporter 建议，也不得为非阻塞差异机械重跑。Writer 只能提交带完整运行与图像证据的 `ready_for_review`；最终 `matched` 由 Reporter 直接核验论文后授予。

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

case 产物默认集中到 %USERPROFILE%\Desktop\耿同学agent_cases。因此 --out case_001、
status case_001 和 benchmark case_001 ... 中的相对名称都会从该目录解析，不会再在源码仓库根目录
创建运行产物。需要临时改位置时设置 GENG_CASES_ROOT；显式绝对路径仍会按原样使用。

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
--mineru-timeout 1800        MinerU 单篇预解析超时；超时后自动回退页面图定位
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

安装 web extra 后启动，前端静态文件已经随 Python 包提供。Windows 日常运行建议使用持久启动脚本，它会把服务放到独立后台进程，不继承调用终端或自动化工具的执行时限：

```powershell
.\start-web.ps1
```

`geng-agent-web` 仍可用于前台调试。Web 服务和本地 Celery 作业均不设置固定 hard/soft time limit；持久启动日志及 PID 写入 `GENG_CASES_ROOT\.web\`。

浏览器打开：

```text
http://127.0.0.1:8765
```

Web UI 支持上传 PDF、管理案例、查看五个当前主流程阶段、接收 SSE 实时事件、预览阶段产物并导出 ZIP。运行期间每 10 秒增量更新一次产物索引，每个步骤或阶段完成时也会立即同步；任务失败、取消或重试前仍会保留并展示已经落盘的阶段产物。第三阶段只索引 writer 的结果图、CSV、summary、自审结果及 task reporter 的目标裁图和核验结果，不索引大体积 transcript 与重复论文页。五个阶段对应“论文解构、复现设计、任务级复现、报告编排、交付物生成”，不再依赖旧流水线的私有方法名。

本地默认使用 SQLite 和进程内 Celery eager worker；数据库位于 case 根目录下的 `geng_web.db`。生产部署可通过以下环境变量切换 PostgreSQL、Redis 和外部 Celery worker：

```text
GENG_CASES_ROOT         case 根目录；默认 %USERPROFILE%\Desktop\耿同学agent_cases
GENG_DATABASE_URL       SQLAlchemy 数据库地址
GENG_REDIS_URL          Redis/Celery 地址
GENG_CELERY_EAGER       1 表示本地进程内执行
GENG_ENABLE_URL_IMPORT  1 表示允许受 SSRF 防护的 PDF URL 导入 API
```

当前 Web 尚未实现登录和租户隔离，默认只应通过 `127.0.0.1` 本机访问；不要把服务直接绑定到公网地址。

取消操作采用安全边界协作停止：已经启动的单次外部调用不会被粗暴截断，但进入下一阶段前会停止。Web worker 不额外设置固定科学迭代墙钟上限。为避免案例页一次加载近千个页面图和 transcript，`audit/` 完整保留在磁盘但不进入默认 Web 产物索引；报告、复现代码、结果图、CSV 和顶层证据文件仍可浏览和导出。

## 输出目录

以下结构默认位于 %USERPROFILE%\Desktop\耿同学agent_cases\case_001，而不是源码仓库内：

一次运行会生成类似结构：

```text
case_001/
  paper_chunks.json
  paper_figure_index.json
  engineering_facts_initial.json
  repro_tasks_preliminary.json
  engineering_facts_backfill.json
  engineering_facts.json
  analysis_warnings.json
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
    00_mineru/
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
- `analysis_warnings.json`：前两阶段来源、引用、证据契约和缺失字段的非阻断诊断，供 Writer 继续核对全文。
- `risk_report.json`：可复现性风险、缺失信息、前两阶段兜底、运行异常和审计摘要。
- `audit/`：Codex prompt、stdout/stderr、JSON 校验、运行日志、图片证据等完整审计链。

## 第三阶段任务级 writer 自治循环

每个任务 writer 只拥有自己的 sandbox，允许修改本任务代码、私有配置、README 和 requirements。它必须：

1. 阅读任务事实、论文证据和完整目标图，生成本任务复现代码与配置。
2. 自行探测硬件并选择 CPU/GPU、并行度、批量大小和依赖。
3. 在 `--run-repro` 开启时直接运行自己的 smoke/full。
4. 先核验论文明确的数据、模型、公式、核心算法和实验协议，再对照曲线、baseline、数值形状、坐标轴、统计量与呈现细节。
5. 只在论文未披露或确有歧义处提出显式合理假设；材料性问题存在具体修正方向时修改并重跑，非材料差异只记录，禁止机械重复 full。
6. 成功 full 已忠于明确事实、支持任务核心观点且只剩公开假设或非材料差异时输出 `ready_for_review`；Writer 不能输出最终 matched。

主持人同时启动所有 writers。每个 Writer 交付后，只有其对应的 task reporter 会立刻在独立上下文中审查该任务；科学差异仅让该 Writer 在原 sandbox 继续修改和 full，其他任务不受影响。所有任务通过后，Final Report Editor 只读取已验收任务包与图片，负责三份人工报告的语言组织与排版。

## 安全边界

- 生成代码、论文文本、日志、stdout/stderr 和图片内容都按不可信输入处理。
- 依赖必须通过 allowlist 和 reconciliation。
- 静态扫描会检查高风险文件操作、系统命令、网络行为等。
- 每个 writer 使用独立 sandbox 隔离任务文件；运行权限和资源决策由 writer 自己负责。
- 主持人会确定性清理 Python BOM；静态扫描发现语法错误时 runtime 不得显示通过。
- `matched` 要求 Reporter 确认明确论文事实未被违反、任务核心观点得到本地结果支持且 full 证据可信；论文未披露部分允许采用公开、合理的假设，剩余非材料差异不阻断通过。

## 项目定位

耿同学 agent 的目标是提供忠于论文证据并能检验核心观点的复现结果，不是替代人工科研判断。`matched` 只表示明确事实与核心观点经独立 task reporter 核验通过；它允许对论文未披露细节作出透明、合理的工程假设，不表示恢复了作者未公开代码。
