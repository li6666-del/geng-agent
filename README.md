# 耿同学 agent

面向通信论文的自动复现与可信审查工具。它不是“论文真伪裁判”，而是把一篇通信论文拆成可追溯事实、可运行任务、任务级复现实验、图像证据对比和人工可读报告，帮助研究者更快判断复现结果是否支持论文结论，以及差异可能来自哪里。

当前项目生成与复现主线只使用 Codex CLI；OpenAI-compatible LLM 仅保留为前两阶段论文分析的显式兼容选项。
Case 工作流只接受 `workflow_version: "2"`。已有阶段产物但缺少有效 V2 marker 的目录不会被原地升级；请在新的干净 case 目录重建。

开发与验证默认在本机执行，使用本机已有的合适 Python 环境。远端同步、SSH 和远端验证不再是前置要求；`tools/remote_*` 仅保留作明确需要时的可选工具。执行约定见 `AGENTS.md`。

本轮通用复现整改的实现范围和实际验证记录见 [实施计划](docs/reproduction_remediation_plan.md) 与 [验证记录](docs/remediation_validation_20260906.md)。


## 当前能力

- 解析 PDF/TXT/Markdown 论文，保留全文分块、页面图像和带 caption/page/bbox 的图候选索引，供 Codex 直接查证。
- 一个 Codex 事实专家只做一轮全局事实扫描，一个任务专家只做一轮初步设计并立即输出 `backfill_handoff`；只有明确点名的实验定义 blocker 才进入定向回补，通常 0–2 轮，第三轮仅作异常熔断。
- 定向回补后抽取论文核心主张、作用机制、方法排序和限制，再由 Task Designer 生成最终任务；每个任务内嵌唯一的 `scientific_acceptance` 契约，以稳定 ID 描述核心结论、关键数值目标和信息缺口。
- 将论文图表拆成可运行的复现实验任务；论文证据负责选择“什么结论重要”，宿主只执行统一的材料性策略和数值计算，不用像素、配色或版式替代科学判断。
- 任务定稿后生成 `scientific_architecture.json`，统一约束跨任务共享的系统模型、数量形状、单位、归一化、组件接口、不变量及验收 criterion 到输出量的绑定。新契约会使旧 case 缓存失效，不提供旧结构兼容层。
- `scientific_architecture/1.1` 由 Architecture Agent 按组件选择真实运行栈、设备策略、精度、训练/梯度/检查点能力和共享边界；类型与框架均不绑定通信领域或 PyTorch。宿主能力只决定“当前能否执行”，缺包、缺 GPU 或未启用的运行时会形成显式 capability gap，不能触发 NumPy/CPU/占位实现的静默降级。
- Task Designer 显式声明任务间的执行关系：`strong` 关系编译为同一个 execution unit、同一个 Codex Writer/sandbox/run；`weak` 关系保持独立 execution unit，只在跨 unit 时要求共享冻结的科学定义。逻辑任务始终保留独立验收与独立 Reporter。
- Foundation Writer 仅为确需跨 execution unit 一致的组件及其依赖生成共享 `src/` 与契约测试，并形成按内容哈希冻结的 canonical snapshot；没有跨单元共享需求时直接跳过，任务私有实现保持可修改。
- 第三阶段按 execution unit 启动自治 writer：互不相关的 unit 并行，compound Writer 在一个沙箱内按依赖顺序完成所有成员任务，并为每个逻辑任务分别交付结果。
- 每个 writer 把论文明确事实和任务验收 ID 作为最高约束，只在论文未披露或确有歧义处作显式工程假设；它保留“运行—比较—修改—重跑”的自迭代，但只能因运行无效、核心结论失败或关键数值对称倍率达到 10 倍而重跑，且必须给出证据对应的因果修改计划。
- 主持人只做任务覆盖、证据路径和基础代码健康检查，不参与科学结论。
- 每个 writer 交付后立即启动对应的独立 Codex task reporter，按 criterion ID 核对论文与本地产物。宿主可得出 `reproduced`、`reproduced_with_assumptions`、`inconclusive_missing_information` 或 `not_reproduced`；后两者是可报告终态，不会被当作流水线故障或逼迫 Writer 无限重跑。所有任务进入终态后启动 Final Report Editor。
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
  -> 有 blocker 时只回补选中项，同一未解字段最多搜索两次；没有 blocker 时直接进入主张提炼
  -> Python 只硬验收 JSON/基本结构；来源、引用、证据类型和缺失字段写入 analysis_warnings.json，不触发科学内容重写
  -> Codex analysis 基于最终事实抽取 paper_thesis.json；Task Designer 随后生成唯一、权威、带 scientific_acceptance 的最终任务契约
  -> experiment_index v2 记录任务、图表、参数、baseline 和证据定位，不做运行前复现评级
  -> Architecture Agent 生成 scientific_architecture.json，并由机器检查跨文档引用、形状/单位作用域和 task binding
  -> 宿主把 strong 关系编译为 compound execution unit，把 weak 关系保留为可重叠但不传递闭包的共享定义组
  -> 跨 unit 的 weak 关系由 Foundation Writer 生成共享科学模块与 tests；验证、哈希冻结后复制到相关 Writer sandbox
  -> 为每个 execution unit 创建 sandbox；所有互不依赖的 unit Writer 同时启动，compound Writer 在同一 sandbox/run 中完成其成员任务
  -> 每个 writer 按验收 ID 自行比较并迭代；只有运行无效、核心结论失败或关键数值倍率达到 10 倍才允许带因果计划重跑
  -> 每个任务完成后立刻启动独立 task reporter，提交 criterion 级观察；宿主计算倍率并决定复现、带假设复现、信息不足或未复现
  -> 可用时从原 PDF 高分辨率裁切；边界不可靠、无 crop 或无图任务直接使用父图或 CSV/表格/summary/文本证据，不回退 Writer
  -> 全部任务进入可报告终态后，Final Report Editor 统一组织三份报告；只有全部成功复现时才授予 matched
  -> 输出 review、reproduction_report、result_review 的 Markdown/Word 版本
```

## 科学结果与执行证据

最终任务稿按任务完整规格发布，可以撤销旧参数、假设和关系；此前稿件仍保存在 audit。独立 Reporter 保留任务清单之外的新反证，成功判断必须有实际本地证据。原始输出、源码与论文输入在审查前后核对完整性，最终报告附宿主生成的任务终态。

Writer 使用生成项目的 `run_task.py --task <ID> --config <配置> --mode full` 提交一次实际执行。宿主记录进程退出状态、可观测源码/配置/输入、运行环境和输出哈希；原生库加载的数据须在配置或 `--input` 中声明，smoke 不得作为 full 证据。每次开始前归档该任务旧输出，避免空运行继承旧 CSV。执行后新增数值产物不算本次运行证据；补画图片可用于排版，但科学判断仍需已观察的测量或源码证据。原始运行记录在 `audit/execution_runs/`，交付中的 `execution_evidence.json` 解释配置改名与文件搬移，不伪称组装后的文件曾重新执行。

科学子进程通过本机 `codex sandbox` 执行，只有任务输出、共享产物和运行缓存目录可写。该命令不调用模型，也不修改用户的全局配置；CLI 缺少所需沙箱能力时公开失败，不静默取消隔离。Python 读取与环境保护继续生效，Windows 当前后端不提供完整原生读取隔离。

Foundation 只冻结需要跨执行单元一致的共享组件与依赖。私有科学实现可由所属 Writer 修改；共享缺陷通过有论文证据的 `foundation_revision_request.json` 定点修订。共享训练产物必须有生产者和消费关系。缓存按执行单元的科学规格、相关组件与依赖版本判断；缺包、局部修订和一次无进展重试不再清空整个 case。

交付包附安装文件、已记录的依赖版本、任务配置和执行证据，并分别验证目录搬移和独立虚拟环境运行。环境重建失败会明确记录，科学未复现也保留其真实终态。成本事件按调用保存并跨恢复累计；缺失的历史用量保持未知。

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

`.[repro]` 只是常见通信论文的便利预装资料包，包括 `numpy`、`scipy`、`matplotlib`、`pandas`、`sympy`、`numba`、`torch`、`scikit-learn`、`galois`、`h5py` 等；它不是包准入白名单。每个 case 复用宿主选定的共享 Python：解析器先对全部请求包做真实版本和 import 探测，只从登记的 HTTPS 来源补装未满足的二进制 wheel，并在 case 动态锁中逐项记录 `host_runtime` 或 `trusted_index` 来源。宿主随后执行 `pip check` 和真实科学能力探针；共享运行时的完整变更事务由宿主级互斥锁串行化。

安装后先自检：

```bash
python -m geng_agent doctor
```

`doctor` 会检查 Python 版本、运行本体依赖和常用复现资料包。只有 Python/编排器依赖缺失才阻断；论文特有库由 Case Resolver 在执行前补齐、锁定并验证，不会迫使架构设计师改用更弱实现。

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

系统会把原始论文文件、全论文页面图以及最终定稿的 `engineering_facts.json`、`repro_tasks.json`、`execution_plan.json`、`experiment_index.json`、v2 的 `scientific_architecture.json`、可选 `paper_thesis.json` 和 `analysis_warnings.json` 复制到每个 writer sandbox。确需共享科学层的 sandbox 安装同一份只读 Foundation snapshot。所有论文页面图直接随 Codex writer 会话发送，不再执行任务页筛选；任务相关事实摘要只用于文本导航，不构成信息边界。

每个 writer 交付后，专属 task reporter 在独立上下文中读取该任务的 `task_agent_result.json`、执行摘要、本地 PNG/CSV/summary 和完整论文证据，按稳定 criterion ID 提交观察。宿主统一计算关键数值的对称倍率：两个有限、非零同号量的倍率小于 10 时，单纯参数差异不构成重跑理由；零值、符号、阈值或更严格数值准确性只有在任务把它明确写成核心结论时才按该结论裁决。裁图或证据定位问题只重跑对应 Reporter，绝不重跑 Writer。所有任务进入终态后，Final Report Editor 汇总生成三份最终报告。

对 PDF，系统优先把 MinerU 的整图候选连同 caption、页码和归一化 bbox 交给 task reporter。Reporter 可在候选父图内部标注目标子图，Python 再从原 PDF 确定性裁切；任一边界不确定时使用完整父图。图像不是全局必需产物：图类任务应有可读结果图或等价的 CSV、表格、summary、文本证据，无图任务和信息不足终态可直接用结构化证据成文。

每个 task reporter 都拥有独立工作区，绝不接收其他实验的本地产物或结论；最终编辑器没有科学裁决权。任务级或编辑器级进程失败会分别记录在对应 audit 状态中。

## Codex 配置

默认全流程走 Codex CLI：

```bash
set GENG_CODEX_CMD=codex
```

也可以按阶段覆盖：

```bash
set GENG_CODEX_ANALYSIS_CMD=codex
set GENG_CODEX_FOUNDATION_WRITER_CMD=codex
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
set GENG_CODEX_FOUNDATION_WRITER_REASONING_EFFORT=xhigh
set GENG_CODEX_TASK_WRITER_REASONING_EFFORT=xhigh
set GENG_CODEX_TASK_REPORTER_REASONING_EFFORT=xhigh
set GENG_CODEX_REPORT_EDITOR_REASONING_EFFORT=xhigh
```

task writer 采用全任务并发：有多少复现任务就同时启动多少个 writer。每个 writer 在自己的 sandbox 内直接调用当前 Python，自行探测 CPU/GPU、选择 backend、声明依赖并运行 smoke/full；主持人不做资源排队或科学判断。项目不对 Codex 推理会话设置 wall-clock 上限；会话只在正常完成、明确失败或用户停止时结束。后续迭代只在材料性原因成立且有具体因果修改方案时启动。

Writer 必须先读取 `repro_tasks.json` 的 `_meta.fact_gap_handoff`，再从抽取事实、原论文 PDF、caption、正文、公式、表格和附录中补找缺失参数；前两阶段的未解决记录只是导航，不是停止依据。论文明确给出的数据、模型、公式、算法和实验协议不可为了贴图而改动；仍找不到的值或实现细节才允许成为可追踪、可调整的科学假设。对 Monte Carlo、批量矩阵运算和分钟级 CPU full，CUDA 可用时应优先实现真实 Torch CUDA 计算路径；仅调用 backend selector 或在报告中写 GPU 名称不算使用 GPU。

Writer 使用直接的对比迭代：运行 full 后逐项核验 `scientific_acceptance`。只有 `invalid_run`、`core_conclusion_failed` 或 `key_numeric_ratio_ge_10` 三类材料性原因，且存在论文证据、受影响 criterion ID、具体修改目标和可预测影响时，才修改并重新 full；配色、字体、线宽、布局、像素、裁图紧凑度及其他呈现差异不得触发重跑。若运行有效但结论不支持，或论文信息不足且没有证据支持的下一步修正，Writer 应立即交给 Reporter 形成 `not_reproduced` 或 `inconclusive_missing_information`，不无限自改。

恢复缓存采用内容寻址：论文 PDF 内容、最终任务契约、相关输入、schema、prompt 与宿主策略版本任一变化都会使对应缓存失效。同一路径替换 PDF 不会复用旧解析；旧 case 无迁移兼容承诺。

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
--run-timeout 120            单次任务运行超时
--no-resume                  不复用已有阶段产物，从头运行
```

Analysis、Foundation/Task Writer、Task Reporter 和 Report Editor 的 Codex 推理会话均不设项目内运行时长上限。上面的 `--mineru-timeout` 与 `--run-timeout` 约束的是非 Codex 子流程。

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
  workflow.json
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
  scientific_architecture.json
  foundation_manifest.json
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
    03b_foundation_snapshot/
    03c_task_writer_sandboxes/
    03c_task_writers_*.json
    04_reporter_*.json/md/txt
    04_reporter_workspace/
  repro_project/
    README.md
    requirements.txt
    execution_plan.json
    artifact_lineage.json
    environment.lock.json
    reproducibility_manifest.json
    source_inventory.json
    tasks_manifest.json
    configs/
    execution_units/
    src/
    tasks/
    tests/
    outputs/
```

其中：

- `review.md/docx`：主报告，概述事实、任务、运行、风险和结果审查状态。
- `reproduction_report.md/docx`：逐任务记录本地代码实际采用的关键参数、随机种子、后端、统计设置和显式假设。
- `result_review.md/docx`：逐任务展示可用的本地复现图、论文原图或等价结构化证据，并给出 criterion 级终态、原因与不确定性；不包含 writer 自我迭代附录。
- `verification_result.json`：Reporter 观察和宿主派生的逐任务终态；`inconclusive_missing_information` 与 `not_reproduced` 是正常可报告结果。
- `runtime_result.json`：执行摘要、产物汇总和最终独立验收状态；所有任务进入终态即可报告，只有全部成功复现才计为 matched。
- `analysis_warnings.json`：前两阶段来源、引用、证据契约和缺失字段的非阻断诊断，供 Writer 继续核对全文。
- `risk_report.json`：可复现性风险、缺失信息、前两阶段兜底、运行异常和审计摘要。
- `audit/`：Codex prompt、stdout/stderr、JSON 校验、运行日志、图片证据等完整审计链。

## 第三阶段任务级 writer 自治循环

每个 execution-unit Writer 只拥有自己的 sandbox。singleton Writer 负责一个逻辑任务；compound Writer 负责一组不可科学拆分的任务，并共享同一次运行的状态、数据划分、随机实现或生产者产物。它必须：

1. 阅读任务事实、论文证据、`execution_unit.json` 和每个成员的 `scientific_acceptance` 契约，生成该 unit 的复现代码与配置。
2. 自行探测硬件并选择 CPU/GPU、并行度、批量大小和依赖。
3. 在 `--run-repro` 开启时按 execution plan 的依赖顺序运行 smoke/full；共享数据、检查点和状态写入稳定的 unit 命名空间。
4. 先核验论文明确的数据、模型、公式、核心算法和实验协议，再按稳定 ID 对照核心结论和关键数值目标。
5. 只在论文未披露或确有歧义处提出显式合理假设；三类材料性原因之一成立且存在具体因果修正方向时修改并重跑，其他差异只记录。
6. 核心结论得到支持且关键数值倍率小于 10 时立即输出 `ready_for_review`；运行有效但忠实结果失败或无法判断时也应停止自改并提交 Reporter。Writer 不能输出最终 matched。

主持人同时启动所有 execution-unit Writers。每个逻辑任务仍有且仅有一个 task reporter；compound unit 中任一 Reporter 指出共享科学的材料性缺陷时，整个 unit 才在原 sandbox 中作一次有因果依据的续跑，其余 unit 不受影响。所有任务进入终态后，宿主冻结包含源码、配置、数据/检查点、环境锁、artifact lineage 和 source inventory 的便携项目，再由 Final Report Editor 负责三份人工报告的语言组织与排版。

## 安全边界

- 生成代码、论文文本、日志、stdout/stderr 和图片内容都按不可信输入处理。
- Python 包不使用静态准入白名单。Writer 只能提交普通 PEP 508 依赖请求，不能提供 URL、索引或安装参数；宿主对已有包做真实探测，只从登记的 HTTPS 来源补齐缺失 wheel。动态锁同时绑定宿主运行时 provenance、请求包的版本/import 结果，以及新安装 artifact 的 SHA-256；Writer 不得自行运行安装器。
- 静态扫描会检查高风险文件操作、系统命令、网络行为等。
- 每个 writer 使用独立 sandbox 隔离任务文件；运行权限和资源决策由 writer 自己负责。
- 主持人会确定性清理 Python BOM；静态扫描发现语法错误时 runtime 不得显示通过。
- `matched` 要求全部任务的有效 full 支持核心结论，所有可比较关键数值的宿主计算倍率小于 10；论文未披露部分允许采用公开、合理的假设。样式、像素和裁图差异不参与该结论。

## 项目定位

耿同学 agent 的目标是提供忠于论文证据并能检验核心观点的复现结果，不是替代人工科研判断。`matched` 只表示明确事实、核心观点和宿主管理的材料性数值门槛通过；`inconclusive_missing_information` 与 `not_reproduced` 则如实保留信息不足或忠实失败的科学结果。任何终态都不表示恢复了作者未公开代码。
