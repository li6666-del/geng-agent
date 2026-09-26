# 耿同学agent 项目架构报告（当前源码版）

> 更新日期：2026-07-16。本文档以当前源码、CLI 和测试为准。

## 1. 项目定位

耿同学agent 是面向通信论文的本地工程复现与证据审查系统。它把论文转换为可追溯事实、可运行任务、任务级复现代码、论文/本地图像证据和人工可读报告，不直接判断论文真伪。

当前核心原则：

- 前两阶段采用前置软交接：一轮全局事实抽取、一轮初步任务设计后立即判断是否可交给 Writer；只有明确选中的实验定义 blocker 才进入定向回补，通常 0–2 轮，第三轮仅作异常熔断。
- 论文解析后先建立带实体 ID、子图、公式、表格与交叉引用的 Paper Memory，并用快照哈希锁定第三轮输入。
- `paper_thesis.json` 无条件抽取，向后续 writer 提供中心主张、机制、方法排序和适用区间。
- 第三阶段使用任务级 Codex Writer 与对应的隔离 Task Reporter，不再保留全局 writer、harness runner、全局审查线程或模板项目路径。
- 一个任务对应一个 writer；所有 writer 同时启动并直接运行本任务 full，把论文明确事实作为最高约束，只在论文空白处作显式假设，核心观点得到支持后提交 `ready_for_review`。
- 任务目标以论文原文和原图为准；实验索引只提供任务、参数、baseline 和证据定位导航。
- 每个任务配置一个独立 Codex task reporter，按材料性门槛把明确事实冲突或核心观点失败定向回流给对应 Writer；合理假设和非材料差异不阻断通过，全部通过后再授予 `matched` 并生成报告。

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
- `GENG_CODEX_TASK_REPORTER_CMD`：任务级科学审查 agent 覆盖命令。
- `GENG_CODEX_REPORT_EDITOR_CMD`：最终报告编辑 agent 覆盖命令。
- `GENG_CODEX_MODEL`：项目子智能体模型覆盖；未设置时固定为 `gpt-5.6-sol`，与桌面 Codex 全局默认配置隔离。
- `GENG_CODEX_ANALYSIS_REASONING_EFFORT` / `GENG_CODEX_TASK_WRITER_REASONING_EFFORT` / `GENG_CODEX_TASK_REPORTER_REASONING_EFFORT` / `GENG_CODEX_REPORT_EDITOR_REASONING_EFFORT`：默认均为 `xhigh`。

OpenAI-compatible LLM 只保留为前两阶段显式兼容路径；第三阶段没有 LLM backend。

## 3. 当前流水线

1. **论文解析**
   - `documents.py` 读取 PDF/TXT/Markdown。
   - PDF 同时提取文本块并渲染页面 PNG。

2. **论文理解**
   - 同一分析调用读取原文并生成事实和核心主张，保留事实来源、假设和未解决信息。
   - 原始交付直接传递；宿主不按事实条数、字段措辞或科学内容拒收。

3. **联合规划与定向补查**
   - 同一规划者决定最终任务清单和可选科学架构；同时权衡科学依赖、单个 Writer 工作量、独立验收与修订能力和并行收益，在计划说明中简述划分理由。共同实现的小变体可合并，工作量明显不同且可独立完成的目标可拆分；保留全部必要目标、条件和分别的结果，减少无关附加实验。
   - 只有规划者提出的阻塞缺口进入补查；主持人决定例外恢复，规划者联合修订任务和架构。
   - 宿主不再编译 strong/weak 分组，不设置 Foundation。组件绑定只提供实现上下文。

4. **完整任务实现与文件传递**
   - 每个最终任务分配一个独立 Writer，拥有完整源码、配置、测试及私有环境；无依赖任务并行。
   - 确需传递训练检查点或数据时，消费任务用 `depends_on` 声明生产者和文件；上游交付后复制给消费方，缺失情况也交给消费方。
   - 宿主记录实际运行与异常。日志或收据观测失败不会终止进程，也不替 Reporter 重判结论。用户主动停止仍可终止所拥有的进程树。

5. **独立审查与主持人恢复**
   - 每个 Writer 交付后立即启动对应 Reporter，逐实验核对原文、实现、假设和结果，不等待其他独立任务。
   - 科学结论由 Reporter 给出；主持人决定是否修复、找谁修复或保留当前结果。无宿主固定重试上限、无代码变化指纹批准关卡。
   - 真实无法派发、工具运行失败或读不到必要输入时请求修复；修复工具本身失败也回到主持人，不由宿主伪造停止决定。

6. **按任务交付与两份中文报告**
   - 各任务完整代码、环境说明、配置、上游输入、运行结果和记录分别打包。打包失败时主持人可安排修复或保留部分交付后继续报告。
   - 已有编辑智能体生成两份中文 Markdown，并自行编写、执行排版脚本生成 Word；宿主不代写正文。
   - `reproduction_report.md/docx` 保存详细执行和追溯记录；`result_review.md/docx` 展示任务结论与本地/原文并列结果图，按实际需要介绍差距和人工核查建议。
   - 本轮变更与恢复边界见 [按任务的智能体工作流](docs/task_owned_architecture.md)。

## 4. 关键模块

| 模块 | 当前职责 |
|---|---|
| `pipeline.py` | 主持人总编排、前两阶段分析、Writer/Task Reporter/Editor 阶段衔接 |
| `agentic_task_reporters.py` | 任务级隔离科学审查、论文图定位和裁切 |
| `agentic_report_editor.py` | 已验收任务包的三报告语言组织与排版 |
| `agentic_analysis.py` | Codex analysis 子进程与最多一次、零论文图片的格式专修 |
| `agentic_task_writers.py` | 一个任务一个自治 Codex writer |
| `task_writer_support.py` | sandbox、trusted 文件、证据包、manifest/cache 支撑 |
| `paper_evidence.py` | 任务相关事实/页面选择、论文图像编码、排序锚点 |
| `semantic_merge.py` / `facts_coverage.py` | 语义合并、冲突保留和子图级确定性覆盖 |
| `task_evidence_backfill.py` | 字段级请求去重、部分验收、软诊断、搜索台账和有限重搜 |
| `targeted_backfill_loop.py` | 前置软交接、明确 blocker 的选择性回补和三轮异常熔断 |
| `experiment_index.py` | 无预评级的实验实体、参数、baseline 与证据缺口索引 |
| `provenance.py` / `benchmark.py` | 自动化来源链与跨 case 离线评测 |
| `task_scripts.py` | task manifest、dispatcher 和 trusted scaffolding |
| `io_runtime.py` | writer 使用的可信产物 IO 与计算后端运行时 |
| `security.py` | 依赖 allowlist、静态扫描、环境隔离和脱敏 |
| `schema_models.py` / `schemas.py` | 当前结构化阶段的 Pydantic 接口定义 |
| `risk_report.py` / `verdict.py` | 风险维度与最终复现结论 |
| `agentic_report_editor.py` / `report_editor_word.py` | 编辑智能体生成 Markdown/Word，宿主只检查 Word 包 |
| `web/` | 本地上传、后台运行、Codex 健康检查和阶段进度 |

已删除的旧编排与语义中间层不再参与当前流程。证据链现在直接使用原论文、全文分块、最终分析产物和图候选索引；缓存仅依据文件内容哈希失效。

## 5. 输出结构

所有 case 默认统一保存在 %USERPROFILE%\Desktop\耿同学agent_cases；相对 CLI case 名称也从这里解析，避免运行产物污染源码仓库。

```text
case_xxx/
  paper_chunks.json
  engineering_facts_initial.json
  repro_tasks_preliminary.json
  engineering_facts_backfill.json
  engineering_facts.json
  analysis_warnings.json
  paper_thesis.json
  repro_tasks.json
  experiment_index.json
  scientific_architecture.json
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
    package_index.json
    task_packages/
      t01_<任务ID>/
        requirements.txt
        configs/
        src/
        outputs/
```

## 6. 运行边界

- 论文内容、模型输出、writer 代码和运行日志均视为不可信输入。
- 第三方依赖与源码仍会被静态扫描；语法错误会阻止 runtime 通过，普通依赖警告保留在风险报告。
- writer 在独立 sandbox 内直接运行，主持人不设置 full 槽位、资源租约、进程限制或科学迭代超时。
- 主持人不替 Writer 或 task reporter 做主观科学审查；task reporter 只按明确事实忠实度、核心观点支持度和材料性门槛核验任务。
- `matched` 表示论文明确事实未被违反、核心观点得到本地结果支持且假设已透明记录，不等同于论文真实性或作者代码等价结论。
