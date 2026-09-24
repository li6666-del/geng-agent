# 耿同学 agent

面向通信论文的自动复现与可信审查工具。它不是“论文真伪裁判”，而是把一篇通信论文拆成可追溯事实、可运行任务、任务级复现实验、图像证据对比和人工可读报告，帮助研究者更快判断复现结果是否支持论文结论，以及差异可能来自哪里。

当前项目生成与复现主线只使用 Codex CLI；OpenAI-compatible LLM 仅保留为前两阶段论文分析的显式兼容选项。
Case 工作流只接受 `workflow_version: "2"`。已有阶段产物但缺少有效 V2 marker 的目录不会被原地升级；请在新的干净 case 目录重建。

开发与验证默认在本机执行，使用本机已有的合适 Python 环境。远端同步、SSH 和远端验证不再是前置要求；`tools/remote_*` 仅保留作明确需要时的可选工具。执行约定见 `AGENTS.md`。

当前全局主持人职责、宿主裁决清理与验证边界见 [主持人实施记录](docs/host_moderator_plan.md)。此前通用复现整改记录见 [实施计划](docs/reproduction_remediation_plan.md) 与 [验证记录](docs/remediation_validation_20260906.md)。

提示词与调用上下文的后续整改见 [修改计划](docs/prompt_context_remediation_plan.md) 和 [本地回归及 21 次静态模型对照](docs/prompt_context_validation_20260906.md)。后者明确区分科学判断、实际 token/缓存用量与完整论文复现的验证边界。


## 当前能力

- 解析 PDF/TXT/Markdown 论文，保留全文分块、页面图像和带 caption/page/bbox 的图候选索引，供 Codex 直接查证。
- 论文理解一次读取原文，同时产出工程事实和核心主张；分别保存 `engineering_facts_initial.json`、`paper_thesis.json`，组合缓存为 `paper_understanding.json`。
- 实验规划同时生成任务、验收导航和科学架构。主张在初次规划前已可用；无缺口路径包含理解、规划两次主要分析调用。正常节点交接不再调用主持人批准。
- 分析智能体的原始交付直接进入下一阶段；宿主不按事实条数、措辞、方法排序、趋势或数值差距预审，也不以分析 JSON 的字段形状否决交接。无法解析的原文和错误一并保留，只有实际消费者无法执行时才交由主持人安排修复。
- 只有规划者明确选中的关键缺口才进入定向回补；理解角色按请求查证，规划者随后共同修订任务与架构。保留搜索台账、旧稿及信息不足，不再另调主张提炼、最终定稿或架构 Agent。
- `scientific_acceptance` 是有来源、稳定 ID 的审查导航；独立 Reporter 以原论文、实际实现与执行证据判断科研结论，程序不按文字相似度或统一倍率阈值重判。
- 联合计划保存为 `experiment_plan.json`，同时发布供下游使用的 `repro_tasks.json` 和可选 `scientific_architecture.json`。私有独立任务可以不单列架构；跨执行单元共享科学定义必须具备相应契约。
- `scientific_architecture/1.1` 由实验规划者按组件选择真实运行栈、设备策略、精度、训练/梯度/检查点能力和共享边界；类型与框架均不绑定通信领域或 PyTorch。宿主能力只决定“当前能否执行”，缺包、缺 GPU 或未启用的运行时会形成显式 capability gap，不能触发 NumPy/CPU/占位实现的静默降级。
- Task Designer 显式声明任务间的执行关系：`strong` 关系编译为同一个 execution unit、同一个 Codex Writer/sandbox/run；`weak` 关系保持独立 execution unit，只在跨 unit 时要求共享冻结的科学定义。逻辑任务始终保留独立验收与独立 Reporter。
- Foundation Writer 仅为确需跨 execution unit 一致的组件及其依赖生成共享 `src/` 与契约测试，并形成按内容哈希冻结的 canonical snapshot；没有跨单元共享需求时直接跳过，任务私有实现保持可修改。
- 第三阶段按 execution unit 启动自治 writer：互不相关的 unit 并行，compound Writer 在一个沙箱内按依赖顺序完成所有成员任务，并为每个逻辑任务分别交付结果。
- 每个 Writer 围绕论文明确事实和任务目标实现，只在论文未披露或确有歧义处作显式假设。Reporter 提出有证据和预期效果的修订，主持人决定是否交回 Writer；Python 不按固定理由标签或字段模板判断修订是否值得执行。
- 宿主按数据依赖推进正常阶段和独立 Writer 批次，记录交付、实际执行与异常；只有下一步无法执行或修复归属不明时才唤醒主持人。科学结论由独立 Reporter 判断，主持人安排异常修复。
- 每个 Writer 交付后立即启动对应的独立 Codex task reporter，核对论文与本地产物。Reporter 给出 `reproduced`、`reproduced_with_assumptions`、`inconclusive_missing_information` 或 `not_reproduced`；后两者是可报告终态。协调停止时仍保留未接受意见及停止原因，交由已有的报告编辑智能体说明。
- 主要交付为本地复现报告 `reproduction_report.md/docx` 和论文对比报告 `result_review.md/docx`；`review.md/docx` 是可选导航。两份正文与 Word 版式由编辑智能体完成，宿主检查文件并保存交付；导航缺失不阻碍正文交付。
- 提供极简 Web UI，可上传 PDF 或填写 PDF 链接并实时查看阶段进度。

## 工作流

```text
论文 PDF/TXT/Markdown
  -> 文本分块、页面图；可选 MinerU 图定位
  -> 论文理解：工程事实 + 核心主张（同一调用、分开文件）
  -> 实验规划：任务 + 验收导航 + 科学架构（同一调用）
       -> 有阻塞缺口：理解角色定向查证 -> 原规划角色联合修订
          主持人决定搜索或定稿；沿用搜索台账及三轮资源预算，保留未解决项
       -> 无阻塞缺口：发布联合计划及下游文档
  -> 宿主编译 strong/weak 关系、实验索引，检查可执行依赖
  -> 确需跨执行单元共享时：Foundation 编写共享模块并冻结
  -> 每个执行单元的 Writer 实现论文并提交宿主观测的 full 结果
  -> 每个逻辑任务的独立 Reporter 查原文、代码及执行证据，给出科学结论与解释
       -> 有证据和因果修改方案：相应 Writer 迭代 -> 独立核验
       -> 无可验证的下一步：保留未复现、信息不足或工程故障终态
  -> 汇集已接受结果和停止原因：按实际文件与执行收据组装交付项目
  -> 已有报告编辑智能体撰写中文报告，按任务解释事实、假设、结果图与差距、人工核查建议
       -> 缺少报告文件时由同一编辑角色修复，Python 不补写报告正文
  -> 输出 reproduction_report、result_review 的 Markdown/Word 版本，review 为可选导航
上述箭头表示数据依赖：宿主推进正常阶段和独立批次，主持人决定非例行的补查与修复，Python 执行并记录事件
局部工程故障：保留已验证成果并继续可行报告，交付标记 complete/partial/blocked
```

## 科学结果与执行证据

最终任务稿按任务完整规格发布，可以撤销旧参数、假设和关系；此前稿件仍保存在 audit。独立 Reporter 保留任务清单之外的新反证，并根据可用的本地证据判断科学结论。宿主另行记录每项 full 运行是否被实际观察到，不以收据缺失改写 Reporter 的原判；两种状态并列交给最终报告说明。原始输出、源码与论文输入在审查前后核对完整性。

Writer 使用生成项目的 `run_task.py --task <ID> --config <配置> --mode full` 提交一次实际执行。宿主记录进程退出状态、可观测源码/配置/输入、运行环境和输出哈希；原生库加载的数据须在配置或 `--input` 中声明，smoke 不得作为 full 证据。每次开始前归档该任务旧输出，避免空运行继承旧 CSV。执行后新增数值产物不算本次运行证据；补画图片可用于排版，但科学判断仍需已观察的测量或源码证据。原始运行记录在 `audit/execution_runs/`，交付中的 `execution_evidence.json` 解释配置改名与文件搬移，不伪称组装后的文件曾重新执行。

科学子进程通过本机 `codex sandbox` 执行，只有任务输出、共享产物和运行缓存目录可写。该命令不调用模型，也不修改用户的全局配置；CLI 缺少所需沙箱能力时公开失败，不静默取消隔离。Python 读取与环境保护继续生效，Windows 当前后端不提供完整原生读取隔离。

Foundation 只冻结需要跨执行单元一致的共享组件与依赖。私有科学实现可由所属 Writer 修改；共享缺陷通过有论文证据的 `foundation_revision_request.json` 定点修订。共享训练产物必须有生产者和消费关系。缓存按执行单元的科学规格、相关组件与依赖版本判断；缺包、局部修订和一次无进展重试不再清空整个 case。

交付项目统一按任务分目录；每个目录带有该任务实际执行单元所需的源码（包括所用 Foundation）、安装文件、已记录的依赖版本、配置、结果和执行证据。强依赖任务若同属一次执行，各任务目录均保留该单元完整材料并标明共同执行关系。根目录只提供索引，不声称存在一套已执行的统一源码。打包失败时主持人可保留原始 Writer/Reporter 结果，明确标记项目交付不完整后继续生成报告。交付阶段不新建独立环境、不重新安装依赖或执行独立环境验证。科学未复现保留其真实终态。成本事件按调用保存并跨恢复累计；缺失的历史用量保持未知。

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

`.[repro]` 只是常见通信论文的便利预装资料包，包括 `numpy`、`scipy`、`matplotlib`、`pandas`、`sympy`、`numba`、`torch`、`scikit-learn`、`galois`、`h5py` 等；它不是包准入白名单。可用 `GENG_SHARED_SCIENCE_PYTHON` 指定预装这些大库的共享 Python，避免每个 Writer 重复安装。每个 case 先记录共享运行时的实际依赖与能力；每个并发 Writer 再获得自己的可写虚拟环境，通过 `.pth` 读取共享库，需要新增包时直接安装进自己的环境。`GENG_WRITER_ENVS_ROOT` 可指定这些虚拟环境的固定存放目录。Writer 的安装互不排队，也不会改动共享基础环境。科学执行使用对应 Writer 的 Python，并记录运行前后的实际包清单；交付项目保留每个执行单元的 `task_requirements/` 和实际运行收据，不打包临时环境。完整交付且报告通过后才清理 Writer 环境；中断、部分交付仍保留以便续跑，后续重建从私有环境锁恢复 Writer 自装的包。

安装后先自检：

```bash
python -m geng_agent doctor
```

`doctor` 会检查启动 CLI 的 Python 版本、运行本体依赖和常用复现资料包；若另设 `GENG_SHARED_SCIENCE_PYTHON`，实际复现基础环境还会在 case 准备时探测。只有 Python/编排器依赖缺失才阻断；论文特有库可由 Case Resolver 在执行前准备，或由 Task Writer 在其私有环境自行安装，不会迫使架构设计师改用更弱实现。

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

每个 Writer 交付后，专属 Reporter 在独立上下文中对照原文、实际代码和宿主 full 收据，按 v3 协议提交科学结论、直接理由、比较条件与下一步动作。Reporter 判断语义等价、数值可比性、材料性与假设影响；任务归属由调度器确定，不要求 Reporter 重复写对任务 ID。只有明确要求 `rerun_writer` 才进入修订调度，其他可读结论直接交给报告阶段。宿主保存原始字段及异常、独立记录运行证据，不按字段措辞或数值阈值改判。只有未交出可读结论才要求修复交接；仍失败则记录 review_incomplete。所有任务到达可报告终态后生成两份详细报告，可选生成导航报告。

对 PDF，系统优先把 MinerU 的整图候选连同 caption、页码和归一化 bbox 交给 task reporter。Reporter 可在候选父图内部标注目标子图，Python 再从原 PDF 确定性裁切；任一边界不确定时使用完整父图。图像不是全局必需产物：图类任务应有可读结果图或等价的 CSV、表格、summary、文本证据，无图任务和信息不足终态可直接用结构化证据成文。

每个 task reporter 都拥有独立工作区，绝不接收其他实验的本地产物或结论；最终编辑器没有科学裁决权。任务级或编辑器级进程失败会分别记录在对应 audit 状态中。

## Codex 配置

全流程使用一份统一模型配置：选定服务商、模型和推理强度后，Analysis、Foundation、Writer、Reporter、Moderator、Editor 全部共用，运行时固定配置并纳入缓存和审计。参见 [模型配置说明](docs/model_configuration.md)、[配置示例](configs/models.example.json) 和 [DeepSeek V4.1 Flash / max 配置](configs/models.deepseek-v4.1-flash.json)。

Moderator 默认只处理异常与非例行选择。宿主按已有数据依赖启动分析、执行、报告及独立 Writer 单元；只有交接无法继续、依赖或共享修订需要判断时才唤醒主持人。运行中仅能等待时直接等待，不再调用模型解释“仍在运行”。Writer 的可读交付连同宿主执行异常交给独立 Reporter；宿主不预先给科学结论。Python 继续负责真实收据、文件归属、写入冲突、取消与预算约束。`GENG_SUPERVISOR_MAX_ACTIONS` 默认 128，限制异常调度会话的动作数量。宿主进程退出后仍需重启工作进程恢复。详见 [主持人设计与实施记录](docs/host_moderator_plan.md)。

```powershell
python -m geng_agent models --config configs/models.example.json
python -m geng_agent review paper.pdf --out case_001 --run-repro --model-config configs/models.example.json
```

第一条命令只做离线配置检查。示例默认 GPT，切换 `default` 可选择 DeepSeek 或 GLM；第三方凭据和 Codex 模型目录需按说明配置，尚未进行真实第三方模型全流程验证。以下环境变量说明适用于未选择模型配置文件的兼容模式。

默认全流程走 Codex CLI：

```bash
set GENG_CODEX_CMD=codex
```

最终报告默认使用已有的 Codex 报告编辑智能体（Report Editor）。旧 `GENG_REPORT_MODE` 选择开关已移除，Python 程序报告与模板补写路径已清理。Editor 沿用项目指定模型及推理强度；独立 Reporter 保留科学判断权。编辑失败或文件缺失时，只修复报告阶段，不重跑科学实验，不用程序文字伪装成智能体交付。

两份详细报告正文均使用简体中文，由同一个最终 Editor 撰写：

- `result_review.md/docx`：逐任务介绍复现目标与结论、核心事实和假设，并优先把可读的本地结果图与原文结果图并排对照。差距和人工核查建议只在确有未解决问题时写；已复现且无重要限制的任务不硬套这两节。多图任务覆盖相关目标图，缺图明确说明，不能伪造原图或猜测配对。
- `reproduction_report.md/docx`：集中保存完整参数及来源、实现与配置、依赖环境、入口命令、运行与迭代摘要、产物和证据索引、交付限制等详细信息。
- `review.md/docx`：可选的简短导航；缺少它不阻止两份详细报告发布。

Editor 自行编写并运行 `report_layout.py`，生成两份可编辑的 Word 文件；脚本随报告保留。宿主只整理材料、按 Reporter 记录的哈希恢复图片、检查 Word 文件可读性并保存智能体产物；不转换格式、不插入终态正文，也不替 Editor 判定表达。来源事实与 Writer 自述明确区分，人工核查建议标为未执行的建议。语言修订不改变科学判决，也不构成 Writer 重跑理由。

也可以按阶段覆盖：

```bash
set GENG_CODEX_ANALYSIS_CMD=codex
set GENG_CODEX_FOUNDATION_WRITER_CMD=codex
set GENG_CODEX_TASK_WRITER_CMD=codex
set GENG_CODEX_TASK_REPORTER_CMD=codex
set GENG_CODEX_REPORT_EDITOR_CMD=codex
```

项目启动的 Codex 子智能体默认使用 GPT-6（`gpt-6-astra`），推理强度统一为 `medium`，不跟随桌面 Codex 的全局默认配置。需要显式指定时可设置：

```bash
set GENG_CODEX_MODEL=gpt-6-astra
set GENG_CODEX_REASONING_EFFORT=medium
```

所有角色共用这份模型与推理设置，不再读取按角色设置的模型名或推理强度。使用 DeepSeek 等服务商时，通过一份项目模型配置统一切换，详见[模型配置说明](docs/model_configuration.md)。

task writer 按相互独立的 execution unit 并发，strong 依赖组成的 compound unit 共用一个 Writer；每个逻辑任务保持独立 Reporter。正常单元交接无需主持人检查；局部异常修复不阻塞其他已启动单元。每个 Writer 在自己的 sandbox 内调用当前 Python，自行探测 CPU/GPU、选择 backend、声明依赖并运行 smoke/full；当前没有新增 GPU 资源排队。项目不对 Codex 推理会话设置 wall-clock 上限；会话只在正常完成、明确失败或用户停止时结束。后续迭代只在材料性原因成立且有具体因果修改方案时启动。

Writer 必须先读取 `repro_tasks.json` 的 `_meta.fact_gap_handoff`，再从抽取事实、原论文 PDF、caption、正文、公式、表格和附录中补找缺失参数；前两阶段的未解决记录只是导航，不是停止依据。论文明确给出的数据、模型、公式、算法和实验协议不可为了贴图而改动；仍找不到的值或实现细节才允许成为可追踪、可调整的科学假设。对 Monte Carlo、批量矩阵运算和分钟级 CPU full，CUDA 可用时应优先实现真实 Torch CUDA 计算路径；仅调用 backend selector 或在报告中写 GPU 名称不算使用 GPU。

Writer 实现论文并提交 full 结果，Reporter 决定科学结论和是否需要继续。再次执行需要 invalid_run、core_conclusion_failed 或 material_numeric_discrepancy，以及论文和本地证据、受影响观察 ID、具体因果修改及预期效果。key_numeric_ratio_ge_10 仅保留为旧接口名称，不再代表宿主阈值规则。样式、裁图、交接和通信问题不重跑科学实验。没有有据可查的下一步修改时，保留未复现或信息不足等终态。

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
--no-resume                  不复用已有阶段产物，从头运行
```

Analysis、Foundation/Task Writer、Task Reporter 和 Report Editor 的 Codex 推理会话，以及 Writer 发起的单次科学任务执行，均不设项目内运行时长上限。`--mineru-timeout` 只约束 MinerU 预解析。

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
GENG_SHARED_SCIENCE_PYTHON  预装常用科学大库的共享 Python 解释器；未设置时沿用启动宿主的 Python
GENG_WRITER_ENVS_ROOT   各 Writer 可写虚拟环境的根目录；建议放在案例目录外的固定运行目录
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
  paper_understanding.json
  experiment_plan.json
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
    package_index.json
    execution_plan.json
    artifact_lineage.json
    reproducibility_manifest.json
    source_inventory.json
    tasks_manifest.json
    task_packages/
      t01_<任务ID>/
        README.md
        run_experiment.py
        requirements.txt
        environment.lock.json
        configs/
        src/
        tasks/
        outputs/
      t02_<任务ID>/
        ...
```

其中：

- `review.md/docx`：主报告，概述事实、任务、运行、风险和结果审查状态。
- `reproduction_report.md/docx`：逐任务记录本地代码实际采用的关键参数、随机种子、后端、统计设置和显式假设。
- `result_review.md/docx`：逐任务展示可用的本地复现图、论文原图或等价结构化证据，并给出 criterion 级终态、原因与不确定性；不包含 writer 自我迭代附录。
- `verification_result.json`：Reporter 原始科学结论、直接理由和独立的工程状态；review_incomplete 表示审查交接或证据未完成，inconclusive_missing_information 表示 Reporter 判断缺少决定性的论文信息。
- `runtime_result.json`：执行摘要、产物汇总和最终独立验收状态；所有任务进入终态即可报告，只有全部成功复现才计为 matched。
- `delivery_index.json`、`audit/supervisor/outcome.json`：本次可用交付及全局结局。`complete/partial/blocked` 表示工程交付状态，与科学“已复现/未复现”分开；旧报告存在不代表本次报告已经完成。
- `audit/supervisor/tools/`、`events/`、`assignments/`：工具目录、逐次调用、唤醒事件和可跨重启恢复的角色修订指令。
- `analysis_warnings.json`：前两阶段来源、引用、证据契约和缺失字段的非阻断诊断，供 Writer 继续核对全文。
- `risk_report.json`：可复现性风险、缺失信息、前两阶段兜底、运行异常和审计摘要。
- `audit/`：Codex prompt、stdout/stderr、JSON 校验、运行日志、图片证据等完整审计链。

## 第三阶段任务级 writer 自治循环

每个 execution-unit Writer 只拥有自己的 sandbox。singleton Writer 负责一个逻辑任务；compound Writer 负责一组不可科学拆分的任务，并共享同一次运行的状态、数据划分、随机实现或生产者产物。它必须：

1. 阅读任务事实、论文证据、`execution_unit.json` 和每个成员的 `scientific_acceptance` 契约，生成该 unit 的复现代码与配置。
2. 自行探测硬件并选择 CPU/GPU、并行度、批量大小和依赖。
3. 在 `--run-repro` 开启时按 execution plan 的依赖顺序运行 smoke/full；共享数据、检查点和状态写入稳定的 unit 命名空间。
4. 先核验论文明确的数据、模型、公式、核心算法和实验协议，再按稳定 ID 对照核心结论和关键数值目标。
5. 只在论文未披露或确有歧义处提出显式合理假设；需要修改或重跑时，主持人根据任务目标、原始证据和可验证的修改方案安排，Python 不按固定原因枚举二次否决。
6. 有可审查的 full 结果后交付 ready_for_review，由 Reporter 结合原文、指标尺度与统计不确定性决定科学结论。Writer 不输出最终 matched，也不凭通用倍率门槛调参至通过。

宿主在资源不冲突时同批启动所有就绪的独立 execution-unit Writers；异常情况下由主持人决定恢复路径。每个逻辑任务仍有且仅有一个 task reporter；compound unit 中任一 Reporter 指出共享科学的材料性缺陷时，整个 unit 才在原 sandbox 中作一次有因果依据的续跑，其余 unit 不受影响。所有任务进入终态后，宿主冻结包含源码、配置、数据/检查点、环境锁、artifact lineage 和 source inventory 的便携项目，再由已有的最终 Editor 撰写中文报告：结果对比报告集中介绍核心事实、假设、成对结果图、差距和人工核查建议，运行与追溯细节放入本地复现报告。

## 安全边界

- 生成代码、论文文本、日志、stdout/stderr 和图片内容都按不可信输入处理。
- Python 包不使用静态准入白名单。Writer 只能提交普通 PEP 508 依赖请求，不能提供 URL、索引或安装参数；宿主对已有包做真实探测，只从登记的 HTTPS 来源补齐缺失 wheel。动态锁同时绑定宿主运行时 provenance、请求包的版本/import 结果，以及新安装 artifact 的 SHA-256；Writer 不得自行运行安装器。
- 科学执行保留原有操作系统沙箱、运行时访问限制、凭据隔离和文件身份校验。退役的 AST 科研推断、导入写法和路径文案检查不再决定流程通过与否。
- 每个 Writer 使用独立 sandbox；Python 负责执行权限、真实资源状态和并发约束，只有异常恢复需要主持人根据这些事实安排工作。
- 宿主可处理 BOM 等确定性的传输问题，并如实记录语法/进程错误；主持人判断错误与任务的关系，必要时交原负责角色修复。实际失败不能被描述成执行成功。
- Reporter 科学意见、异常时的主持人修复决定和宿主执行证据分别保存。数值差异、符号、对数尺度、概率边界与统计波动由智能体结合论文解释；Python 不按十倍阈值、字段齐全程度或关键词重算科学结论。

## 项目定位

耿同学 agent 的目标是提供忠于论文证据并能检验核心观点的复现结果，不是替代人工科研判断。`scientific_all_successful` 汇总 Reporter 的正面结论，`all_full_runs_observed` 独立说明宿主是否观察到全部 full 运行；二者不一致时报告必须保留差异。`inconclusive_missing_information` 与 `not_reproduced` 如实保留信息不足或忠实失败的科学结果。任何终态都不表示恢复了作者未公开代码。

### 实际运行问题整改（2026-09-08）

[整改计划](docs/remediation_plan_20260908.md)记录职责、改动与验证范围。IPC 使用短临时文件和 Windows 长路径 I/O；客户端可从匹配请求状态接收原始收据，通信失败不会覆盖实验成功。最终冻结保留依赖闭包，仅复用运行相关文件身份一致的环境验证。报告固定展示科学判决理由和工程状态，本地图不依赖论文裁图交付；未被 Reporter 选取的图片标为展示附件，不增加科学证据。Foundation 优先读取共享组件上下文，完整原文与架构按需读取；CLI 在 stderr 输出阶段进度，run_cost 分列阶段模型用量及收据执行耗时。
