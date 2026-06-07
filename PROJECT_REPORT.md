# 耿同学agent 技术报告（权威工程参考）

> 本文档面向新接手本项目的工程师，作为代码级权威参考。所有结论基于 `geng_agent/` 真实源码（截至当前 `main` 分支，最近提交 `89a31c6`）。引用一律给出 `文件:行号`。报告只描述代码现状，并显式标出残留 / 休眠 / 陈旧部分。
>
> 生成日期：2026-06-07。仓库根目录：`C:\Users\84475\Documents\耿同学agent`。

---

## 目录

1. [项目概述与核心原则](#1-项目概述与核心原则)
2. [顶层架构与四轮流水线](#2-顶层架构与四轮流水线)
3. [CLI（geng_agent/cli.py）](#3-cligeng_agentclipy)
4. [逐模块详解](#4-逐模块详解)
5. [数据与产物（case 目录布局）](#5-数据与产物case-目录布局)
6. [Schema 真源](#6-schema-真源)
7. [提示词](#7-提示词)
8. [安全模型](#8-安全模型)
9. [多模态能力](#9-多模态能力)
10. [兜底体系与"死命令"哲学](#10-兜底体系与死命令哲学)
11. [模型与配置](#11-模型与配置)
12. [测试](#12-测试)
13. [当前状态与近期重大变更](#13-当前状态与近期重大变更)
14. [已知局限 / 风险 / 改进方向](#14-已知局限--风险--改进方向)
15. [附录 A：模块清单与体量](#附录-a模块清单与体量)
16. [附录 B：残留 / 休眠 / 陈旧清单](#附录-b残留--休眠--陈旧清单)

---

## 1. 项目概述与核心原则

### 1.1 它做什么

耿同学agent 是一个**本地运行**的通信领域论文工程复现审查 CLI。给定一篇通信论文（PDF / TXT / Markdown），它把论文转换成一条可追溯的工件链：

```
论文 → paper_chunks → engineering_facts → repro_tasks(+experiment_index)
     → repro_project（可运行复现项目）→ runtime_result（受限运行结果）
     → result_review（结果级多模态审查）→ risk_report + review.md/docx
```

包元信息：`pyproject.toml:1-5`，`name = "geng-agent"`，`version = "0.1.0"`，`requires-python = ">=3.11"`。入口脚本 `geng-agent = geng_agent.cli:main`、`geng-agent-web = geng_agent.web.__main__:main`（`pyproject.toml:38-40`）。

### 1.2 它明确不做什么

- **不判定论文造假**。系统消息 `geng_agent/pipeline.py:39-45` 明确："你只做可追溯的复现风险评估，不直接判定论文造假。"
- `risk_report.json` 与 `result_review.json` 只表达**复现风险与差异分析**，不给造假结论（`risk_report` 的 `judgement_style="reproducibility_risk_only"`，见 `pipeline.py:1896`；note 见 `pipeline.py:1903`）。
- smoke 通过**不等于**论文已完整复现（`README.md:260`，`pipeline.py:1903`）。

### 1.3 核心哲学："LLM 提议，本地代码强制校验"

`README.md:253-260` 与代码结构一致：

| 角色 | 职责 | 代码落点 |
|---|---|---|
| LLM | 抽事实、设计任务、生成/修复代码、审查结果 | `pipeline.py` 各 `prompt_book.render(...)` + `client.complete(_multimodal)` |
| 本地代码 | Pydantic 结构校验、路径校验、安全扫描、语法/编译校验、运行验证、图像打包、风险汇总 | `schemas.py`、`security.py`、`outputs.py`、`runner.py` |

关键不变量（贯穿全代码）：

1. **论文文本、stdout/stderr、代码片段、表格、图像一律按 `UNTRUSTED DATA` 处理**。统一包裹函数 `wrap_untrusted(label, text)`（`pipeline.py:1744-1745`、`runner.py:592-593`、`result_review.py:832-833` 各有一份相同实现）。
2. **无有效 `source.chunk_id` 出处的事实必被丢弃**，即使其余字段都合法（`facts_normalize.py:319-323`）。
3. **生成的代码默认不自动运行**；只有用户显式 `--run-repro` 才进入受限运行器（`cli.py:46-49` 默认 `run_repro=False`；`pipeline.py:362-368` 给出 disabled 占位结果）。

---

## 2. 顶层架构与四轮流水线

编排器是 `ReviewPipeline`（`geng_agent/pipeline.py:79-1123`），核心方法 `ReviewPipeline.run(...)`（`pipeline.py:147-478`）。注意：尽管 `pyproject.toml:13` 声明了 `langgraph>=0.2` 依赖，**流水线并非用 LangGraph 实现**——`run()` 是手写顺序编排；`langgraph` 仅在 `preflight.py:40` 的 doctor 自检里被检查是否安装，全代码库无 `import langgraph`（见[附录 B](#附录-b残留--休眠--陈旧清单)）。

### 2.1 数据流总览

```
                 paper_path (PDF/TXT/MD)
                        │
              load_paper() → paper_chunks.json        ┌─ _render_paper_images(): PDF 每页渲染成 PNG（多模态）
                        │                              │  （仅 PDF + 客户端支持 complete_multimodal 时非空）
        ┌───────────────┼──────────────────────────────┘
        │  ① extract_engineering_facts.md  (+page images)
        ▼
   engineering_facts.json  ──(本地归一化/部分接受/截断抢救)──> finalize_engineering_facts()
        │
        │  ② build_repro_tasks.md
        ▼
   repro_tasks.json        ──(归一化/部分接受/截断抢救)──> finalize_repro_tasks()
        │
        ├──> experiment_index.json   （②b：本地确定性构建，无 LLM）
        │
        │  ③ 分块生成：
        │     03a generate_repro_project_plan.md  (+page images)   → 计划
        │     03b generate_repro_project_file.md  逐文件（9 个）    → 文件
        │     （--code-review 时对 4 个 src 科学文件逐文件忠实度审查 + 一次重生成）
        ▼
   repro_project/ + repro_project_manifest.json
        │  本地校验 validate_repro_project()（必要文件 + python_compiles）
        │  失败 → 模板兜底 template_project
        │
        │  ③.5（可选 --code-review）：整项目代码忠实度审查 + 返修循环
        │
        │  受限运行（仅 --run-repro）：run_repro_with_repair()
        │     smoke → full 两相；每相返修循环（LLM / OpenHands / hybrid）
        ▼
   runtime_result.json
        │  失败且无部分产物且开启兜底 → 模板兜底 + 重跑
        │
        │  ④（默认开，需 run_repro 成功）：结果级多模态审查
        │     按实验拆分：04a 逐实验 complete_multimodal（喂本地 PNG + 论文页 PNG）
        │     → 04b 本地聚合
        ▼
   result_review.json / result_review.md  (失败 → result_review_error.json)
        │
        ▼
   derive_reproducibility_verdict() → risk_report.json + review.md + review.docx + result_review.docx
```

### 2.2 四轮的精确边界

| 轮次 | 阶段标签 (`stage_label`) | 输入 | 输出文件 | 提示词 | 校验/兜底 |
|---|---|---|---|---|---|
| ① 抽事实 | `01_extract_engineering_facts` | paper_chunks(+page images) | `engineering_facts.json` | `extract_engineering_facts.md` | schema + `validate_fact_sources` + floor + 归一化/部分接受/截断抢救；零事实退关键词兜底 |
| ② 任务 | `02_build_repro_tasks` | facts + paper_context | `repro_tasks.json` | `build_repro_tasks.md` | schema + `validate_task_fact_refs` + 归一化/部分接受/截断抢救；失败退关键词兜底 |
| ②b 实验索引 | `02b_build_experiment_index` | facts + tasks + paper | `experiment_index.json` | **无（本地构建）** | schema |
| ③a 计划 | `03a_generate_repro_project_plan` | facts + tasks + paper_context(+page images) | `audit/03a_*.json` | `generate_repro_project_plan.md` | schema(`repro_project_plan`) + 路径校验 |
| ③b 逐文件 | `03b_generate_repro_project_file_NN_*` | plan + 已生成文件 + facts + tasks + paper | manifest 内 files | `generate_repro_project_file.md` | schema(`repro_project_file`) + 路径/行数/字符/内容类型(ast/json)校验 |
| ③运行 | smoke/full attempts | repro_project | `runtime_result.json` | `repair_repro_project.md`（修复时） | 依赖白名单 + 静态安全扫描 + 产物新鲜度 |
| ④ 结果审查 | `04a_review_reproduction_experiment_NN_*` → `04b_aggregate` | outputs + 论文页 + facts/tasks | `result_review.json/md` | `review_reproduction_experiment.md`（逐实验）+ `review_reproduction_results.md`（overview 仅写 audit） | schema(`result_review_experiment` / `result_review`) |

> 注：`review_reproduction_results.md`（整体版）只被渲染并写入 `audit/04_review_reproduction_results.md`（`result_review.py:69-76`），实际审查走**按实验拆分**的 `review_reproduction_experiment.md`。整体 overview 的 LLM 调用已不再执行。

---

## 3. CLI（geng_agent/cli.py）

`build_parser()`（`cli.py:12-30`）定义 3 个子命令：`review`、`status`、`doctor`。

**惰性 import 设计**：重依赖（pipeline、config、status）在各命令分支内部 import（`cli.py:7-9` 注释说明），目的——即使机器缺少编排依赖，`geng-agent doctor` 仍能跑起来诊断。`main()`（`cli.py:69-122`）：

- `doctor`：`cli.py:73-78`，调用 `preflight.check_environment()`，致命缺失退出码 1，否则 0。
- `review`：`cli.py:80-112`，先 `_warn_if_environment_incomplete()`（stderr 告警，永不阻断，`cli.py:125-136`），构建主客户端（`_build_client_or_error`，`cli.py:139-153`），按需构建异构代码审查客户端（`_build_code_review_client_or_error`，`cli.py:156-183`），再调 `ReviewPipeline(...).run(...)`。
- `status`：`cli.py:114-119`，打印 `inspect_case_status(out)` 的 JSON。

### 3.1 review 参数全表（默认值以 `_add_common_review_args` 为准，`cli.py:33-66`）

| 参数 | 默认 | 作用 |
|---|---|---|
| `paper` (位置) | — | 论文文件，PDF/TXT/Markdown |
| `--out` | 必填 | 输出目录 |
| `--api-key` / `--base-url` / `--model` | None | OpenAI 兼容凭据（覆盖 env） |
| `--max-pages` | None | PDF 最多读取页数 |
| `--temperature` | 0.1 | LLM 采样温度 |
| `--timeout` | 120.0 | 单次 LLM 请求超时（第①轮等） |
| `--tasks-timeout` | 300.0 | 第②轮请求超时 |
| `--project-timeout` | 1200.0 | 第③轮（计划+逐文件）请求超时 |
| `--thinking` | None | `enabled`/`disabled`，DeepSeek V4 Pro thinking 开关 |
| `--reasoning-effort` | None | `low`/`medium`/`high` |
| `--run-repro` / `--no-run-repro` | **False** | 是否显式运行复现（互斥组，默认不运行） |
| `--no-result-review` | False | 在 run-repro 成功后也不做结果级多模态审查 |
| `--no-template-fallback` | False（即兜底默认**开**） | 禁用本地确定性兜底 |
| `--repair-attempts` | 2 | 运行失败后自动修复次数 |
| `--repair-backend` | `hybrid` | `llm`/`hybrid`/`openhands` |
| `--openhands-timeout` | 900.0 | OpenHands 候选修复/验收超时 |
| `--openhands-max-iterations` | 25 | OpenHands 最大迭代步数 |
| `--run-timeout` | 120.0 | 单次复现运行超时 |
| `--json-repair-attempts` | 3 | 每轮 JSON 校验失败的返修次数（实际尝试数 = `json_repair_attempts + 1`，见 `pipeline.py:206`） |
| `--code-review` | False | 代码忠实度审查（默认关） |
| `--code-review-attempts` | **5** | 忠实度审查发现 blocking 后的返修轮数 |
| `--code-review-model` | None | 异构审查模型（默认取 `GENG_CODE_REVIEW_MODEL`） |
| `--code-review-timeout` | **1200.0** | 忠实度审查单次 LLM 超时 |

`review` 还有 `--no-resume`（`cli.py:21`）；`run()` 内部 `resume=not args.no_resume`（`cli.py:100`）。`status` 仅 1 个位置参数 `out`（`cli.py:23-24`）。

> 注意一个细节：`_add_common_review_args(review, include_resume=False)`（`cli.py:20`）只在 `include_resume=True` 时才加 `--resume/--no-resume` 互斥组（`cli.py:51-55`），但该分支从未以 `True` 调用；`review` 子命令单独加了 `--no-resume`（`cli.py:21`）。`include_resume` 参数实际是**休眠分支**。

---

## 4. 逐模块详解

下文每个模块给出职责、关键函数、衔接点。体量见[附录 A](#附录-a模块清单与体量)。

### 4.1 pipeline.py（编排器，2213 行）

核心类 `ReviewPipeline`（`pipeline.py:79`）。构造接收主客户端 `client`、可选 `prompt_book`、可选异构审查客户端 `code_review_client`（`pipeline.py:80-85`）。

**`run()` 编排步骤**（`pipeline.py:147-478`）：

1. 建 `output_dir` 与 `audit/`（`pipeline.py:168-171`）。
2. `_load_or_create_paper()`（`pipeline.py:480-496`）：resume 命中且 `_paper_cache_matches`（按 `source_path` 比对，`pipeline.py:1177-1185`）则复用，否则 `load_paper()`。
3. 计算 `valid_chunk_ids`（`pipeline.py:180-184`），`paper_context`（`_paper_context_for_prompt`，按关键词打分选块，上限 60000 字符，`pipeline.py:1686-1742`），并 `wrap_untrusted`。
4. `_render_paper_images()`（`pipeline.py:881-897`）：仅 PDF + 客户端有 `complete_multimodal` 时，调 `render_pdf_pages_for_llm(paper_path, pages=None, max_pages=60)` 渲染**全部页**；否则返回 `[]`。
5. ① facts：`_load_or_create_stage_json(...)`（`pipeline.py:498-565`），`extra_validation = validate_fact_sources + engineering_facts_floor_issues`，`candidate_normalizer = finalize_engineering_facts`，`truncation_recovery = recover_truncated_engineering_facts`，`fallback_factory = build_fallback_engineering_facts`（仅 `template_fallback` 开时）。
6. ② tasks：同上模板，`extra_validation = validate_task_fact_refs`，`candidate_normalizer = finalize_repro_tasks`，`request_timeout = tasks_timeout`。
7. ②b 实验索引：`_load_or_create_experiment_index()`（`pipeline.py:567-602`），调本地 `build_local_experiment_index`。
8. ③ manifest：`_load_or_create_repro_manifest()`（`pipeline.py:604-668`）→ `_call_chunked_repro_project_generation()`（`pipeline.py:670-797`）。
9. 写盘 + `validate_repro_project`；若必要文件缺失或不可编译 → 模板兜底（`pipeline.py:286-296`）。
10. `build_scientific_check(tasks)`（`pipeline.py:1759-1785`，**仅生成期诊断**，不闸门）。
11. ③.5 `--code-review` 整项目忠实度审查（`pipeline.py:299-314`）。
12. `--run-repro` → `_load_or_run_repro()`（`pipeline.py:839-879`），失败兜底逻辑（`pipeline.py:328-361`）。
13. `_run_result_review_if_ready()`（`pipeline.py:994-1047`）。
14. `build_risk_report(...)`（`pipeline.py:1788-1904`） + `derive_reproducibility_verdict(...)`（`pipeline.py:408-413`）+ schema 自检（`pipeline.py:414-416`）。
15. `_generate_docx_reports()`（`pipeline.py:1049-1123`）+ `render_review_markdown()`（`pipeline.py:2033-2187`）。
16. 写 `review.md`、`risk_report.json`、`generated_files.json`，返回 `PipelineResult`（`pipeline.py:48-60`）。

**分块生成（`_call_chunked_repro_project_generation`，`pipeline.py:670-797`）的关键设计**：

- 先生成计划（03a），`_validate_project_plan_paths`（`pipeline.py:1269-1291`）确保计划覆盖且只覆盖 `REQUIRED_REPRO_FILES`。
- 按 `REPRO_PROJECT_FILE_ORDER`（`pipeline.py:1234-1244`）逐文件生成；顺序保证 `src/simulation.py` 在 channel/modulation/metrics 之后，能看到真实接口。
- 每文件用 `_generated_files_context`（`pipeline.py:1360-1379`，上限 120000 字符，注入**已生成文件全文**）。
- `--code-review` 时，对 `science_files = {src/channel.py, src/modulation.py, src/metrics.py, src/simulation.py}`（`pipeline.py:705`）做"生成→单文件忠实度审查→（有 blocking 则）一次带反馈重生成"（`pipeline.py:713-764`，`review_round` 范围 `range(2)`，即最多 1 次重生成）。反馈经 `review_feedback_json` 槽位回灌。
- 单文件校验 `_validate_project_file`（`pipeline.py:1294-1313`）：路径必须等于 `target_path`、Python 不能空、行数/字符上限（见 `REPRO_PROJECT_FILE_LIMITS`，`pipeline.py:1247-1257`，**全部 200 行**），并 `_content_type_issues`（`pipeline.py:1316-1342`）做 **ast.parse（.py）/ json.loads（.json）内容类型校验**——这是"逐文件 ast/json 校验"的落点，目的是让单文件错误只触发该文件重生成，而不是整项目退模板。

**兜底相关**：`_write_template_repro_project`（`pipeline.py:799-821`）；运行失败兜底 + `_assess_partial_success`（`pipeline.py:1410-1428`，有部分有效 CSV 则保留生成项目）。

**断点续跑**：`_load_valid_stage_cache`（`pipeline.py:1188-1216`）、`_recover_manifest_from_audit`（`pipeline.py:1463-1480`，从 audit 原始输出恢复 manifest）、`_load_cached_runtime_result`（`pipeline.py:1382-1401`）、`_load_cached_result_review_status`（`pipeline.py:1431-1460`）。`_clear_stage_outputs`（`pipeline.py:1515-1641`）按阶段级联清理下游产物 + audit。

**`_call_validated_json`（`pipeline.py:914-992`）**：统一的"调用→解析→归一化→校验→失败返修"循环。`_complete_maybe_multimodal`（`pipeline.py:899-912`）：有 images 且客户端支持多模态则走 `complete_multimodal`，任何多模态失败回退纯文本。`_is_non_retryable_llm_error`（`pipeline.py:1754-1756`）：401/403/unauthorized/forbidden/invalid api key 直接失败不重试。`_temporary_client_timeout`（`pipeline.py:1126-1141`）：临时改 `client.timeout` 给慢阶段更多时间。

**休眠 hook**：`run_stage()`（`pipeline.py:87-145`）和 `_apply_prompt_adjustment()`（`pipeline.py:63-76`）+ `prompt_adjustments` 参数链。`run_stage` 仅被 `tests/test_pipeline.py:285` 用到；`_apply_prompt_adjustment` 注释里提到"监督层补充指令"（`pipeline.py:73`），但已无任何调用方传 `prompt_adjustments`（监督层已移除，见[§13](#13-当前状态与近期重大变更)）。这两个是为已删除的监督层留下的接缝。

### 4.2 runner.py（受限运行器，593 行）

入口 `run_repro_with_repair(...)`（`runner.py:29-101`）：先 `repro_run_phases`（`runner.py:104-107`，有 `config_smoke.json` 且有 `config.json` 则 `["smoke","full"]`，否则 `["full"]`），逐相 `run_repro_phase_with_repair`（`runner.py:110-271`）。任一相失败立即返回失败汇总；全相通过返回成功汇总（含 `repair_attempts_used`、`attempts`、`phase_results`、`artifacts`、`logs_dir`）。

**单相返修循环**（`runner.py:129-257`）：`for attempt_index in range(max_repair_attempts + 1)`：

1. `run_repro_once`（`runner.py:301-384`）：先 `validate_requirements` + `static_scan_repro_project`，任一有问题直接 `blocked_by_security=True` 拒跑（`runner.py:309-331`）；否则 `archive_outputs`（`runner.py:555-564`，保留上次产物）后 `subprocess.run(command, cwd=..., env=build_safe_env(), timeout=...)`（`runner.py:339-347`）。`passed` 条件：returncode==0 且 has_csv/has_png/has_summary_json 且无 invalid_files（`runner.py:349-355`）。
2. 通过即返回；否则若已达 `max_repair_attempts` 跳出。
3. 否则收集 `collect_error_code_context`（`runner.py:497-531`，按 traceback `File "..", line N` 提取出错文件片段，半径 35 行，限定在 repro_project 内），按后端选择修复：
   - `OPENHANDS`：`run_openhands_repair_candidate`（见 §4.10）。
   - `LLM`：渲染 `repair_repro_project.md` → `call_validated_repair_manifest`（`runner.py:440-472`，schema `repair_manifest`）→ `try_repair_candidate`（`runner.py:387-423`，在 `attempt_NN_candidate/` 副本里应用 + 受限运行验收），通过才 `write_file_manifest` 写回主项目。
   - `HYBRID`：先 LLM；LLM 失败或候选不被接受则回落 OpenHands（`runner.py:207-225`、`runner.py:240-256`）。

**命令选择** `choose_repro_command`（`runner.py:426-437`）：smoke 用 `config_smoke.json`，并探测 `run_experiment.py` 是否含 `--config` 决定参数形式。**安全用 `sys.executable`**（即跑流水线的解释器，故必须用 `D:\python`）。

辅助：`build_json_retry_prompt`（`runner.py:475-494`，被 pipeline 复用）、`render_code_excerpt`/`render_file_excerpt`（`runner.py:534-552`）、`ensure_inside`（`runner.py:585-589`，路径逃逸防护）。所有写盘的运行日志都过 `redact_data`（脱敏）。

### 4.3 code_review.py（代码忠实度审查，281 行）

模块 docstring（`code_review.py:1-22`）定位清楚：这是**内容级**审查（公式/星座/Eb-Es/符号等），用 LLM 评审"代码是否忠实实现已抽取 spec（facts+tasks）"，对论文不可知（generality 来自锚定每篇的 facts/tasks 而非硬编码领域断言）。

**双证据 grounding（杀幻觉的关键）**：`filter_grounded_findings`（`code_review.py:75-93`）要求每条 finding 的 `evidence_spec` 必须能在 `facts+tasks` 文本中找到、`evidence_code` 必须能在项目代码中找到，否则丢弃。匹配用归一化 + 滑窗子串（`_norm`/`_grounded`，`code_review.py:60-72`；spec 窗口 8、code 窗口 12，`code_review.py:38-39`）。

**整项目审查 + 返修循环** `run_code_faithfulness_review`（`code_review.py:118-214`）：每轮渲染 `review_repro_project_code.md`，`_call_validated`（`code_review.py:96-115`，schema `code_faithfulness_review`）。只有 `blocking` 触发返修；返修把 blocking 转成 `repair_repro_project_for_review.md` → `repair_manifest` → `write_file_manifest` 就地应用。**返修耗尽不硬失败、不退模板**（`code_review.py:183-191` 返回 `passed=False` + `unresolved_findings`，保留代码、记录残留）。审查无法运行时返回 `passed=None`（`code_review.py:148-157`，不阻断）。

**单文件审查** `review_single_generated_file`（`code_review.py:239-281`）：生成期对单个文件（在内存中、写盘前）做忠实度审查；prior 文件只作参考，grounding 用 `_ground_findings_against`（`code_review.py:217-236`）把代码证据锚定到**当前这一个文件**。任何错误返回无 blocking（不阻断生成）。

### 4.4 result_review.py（结果级多模态审查，837 行）

入口 `run_result_review(...)`（`result_review.py:44-136`）：

1. `collect_result_review_inputs`（`result_review.py:219-257`）：汇总 outputs 下 CSV（`summarize_csv_file`，`result_review.py:558-593`，含数值列趋势）、`summary*.json`、本地 PNG（`encode_png_for_llm`，校验确是 PNG、转 RGBA、缩略到 1600，`result_review.py:637-659`），并 `select_paper_pages`（`result_review.py:662-705`）+ `render_pdf_pages_for_llm`（`result_review.py:708-732`，fitz 渲染，matrix 1.5×，最多 6 页）。**无任何有效 PNG 直接 RuntimeError**（`result_review.py:66-67`），即不存在纯文本替代审查。
2. **按实验拆分**：对每个 repro_task，`select_images_for_task`（`result_review.py:260-318`，按 expected_artifacts / token / 论文页匹配选图）、`compact_result_evidence_for_task`（`result_review.py:321-349`），`call_experiment_result_review`（`result_review.py:139-216`）走 `client.complete_multimodal(...)`（schema `result_review_experiment`）。
3. `aggregate_result_reviews`（`result_review.py:519-542`，**本地聚合**，取最弱标签 `weakest_label`，`result_review.py:545-550`）→ schema `result_review` 校验 → 写 `result_review.json` + `result_review.md`（`render_result_review_markdown`，`result_review.py:749-792`）。

选页/选块逻辑（`select_paper_pages_for_task`、`paper_context_for_task`，`result_review.py:385-499`）按 figure 号、required_facts 的 source.page、关键词打分。`normalize_experiment_review_candidate`（`result_review.py:502-516`）容忍模型把单实验包进 `experiment_reviews`/`experiment_review`。

### 4.5 security.py（安全模型，374 行）— 见 [§8](#8-安全模型) 详解

清单常量：`ALLOWED_REQUIREMENTS`（`security.py:12-20`）、`FORBIDDEN_IMPORTS`（`security.py:33-45`）、`FORBIDDEN_BUILTINS`（`security.py:51-61`）、`FORBIDDEN_CALLS`（`security.py:63-80`）、`SENSITIVE_ENV_KEYS`（`security.py:82-96`）、`SECRET_PATTERNS`（`security.py:98-102`）。核心函数：`build_safe_env`（`security.py:105-121`）、`redact_text`/`redact_data`（`security.py:124-138`）、`validate_requirements`（`security.py:141-170`）、`dependency_policy_prompt_text`（`security.py:173-197`，注入提示词）、`validate_import_requirements`（`security.py:200-243`）、`static_scan_repro_project`（`security.py:323-355`）。

### 4.6 documents.py（PDF/文本→分块，175 行）

`load_paper`（`documents.py:18-36`）：返回 `{source_path, format, chunk_count, chunks}`，chunk 为 `PaperChunk(chunk_id, page, section, text)`（`documents.py:10-15`）。**PDF 优先 fitz**：`_load_pdf`（`documents.py:52-59`）先 `_load_pdf_fitz`（`documents.py:62-93`，`page.get_text("text")`），返回空才回落 `_load_pdf_pypdf`（`documents.py:96-120`，pypdf 缺失才报错）。`split_text`（`documents.py:123-144`，max 6000 / overlap 300，优先在换行处切）。chunk_id 形如 `p{page}_c{idx}`（PDF）或 `text_c{idx}`（文本）。`_guess_section`（`documents.py:156-175`）按首行关键词猜章节。文本读取多编码兜底（`documents.py:147-153`）。支持后缀 `.pdf/.txt/.md/.markdown`（`documents.py:7`）。

### 4.7 config.py（模型客户端配置，109 行）

`get_config_value`（`config.py:9-34`）：先环境变量，Windows 再查注册表 `HKCU\Environment` 与 `HKLM\...\Environment`。`get_cases_root`（`config.py:37-41`，Web 用，默认 `~/Documents/geng_cases`）。`build_llm_client`（`config.py:44-69`，缺 key/model 抛 ValueError，base 默认 `https://api.openai.com/v1`）。`build_code_review_client`（`config.py:72-109`）：仅当 `GENG_CODE_REVIEW_MODEL`（或 `--code-review-model`）设置时返回客户端，否则 None（回落主模型同模型审查）；key/base 仅在 `GENG_CODE_REVIEW_*` 未设时回落 `GENG_LLM_*`。

### 4.8 llm.py（OpenAI 兼容客户端，160 行）

`LLMImage`（`llm.py:10-14`，label/mime_type/data_b64）、`LLMClient` Protocol（`llm.py:17-35`，`complete` + `complete_multimodal`）。`OpenAICompatibleClient`（`llm.py:38-160`，dataclass，字段含 `thinking`/`reasoning_effort`）：

- `complete`（`llm.py:48-77`）：标准 chat/completions；`response_format` 直传。
- `complete_multimodal`（`llm.py:79-122`）：把每张图拼成 `{"type":"image_url","image_url":{"url":"data:<mime>;base64,<b64>"}}`，**关闭 response_format 回退**（`allow_response_format_fallback=False`，`llm.py:117`），即多模态阶段严格要求 json_schema。
- `_post_chat_completion`（`llm.py:124-157`）：用标准库 `urllib`（不引第三方 http 客户端）；HTTP 400/422 且为 `json_schema` 时**自动回退 `json_object`**（`llm.py:142-154`，仅纯文本路径）。

### 4.9 preflight.py（doctor/环境自检，221 行）

模块 docstring（`preflight.py:1-20`）说明两类依赖：编排依赖（`ORCHESTRATOR_DEPENDENCIES`，`preflight.py:35-42`，pypdf/pymupdf(fitz)/pydantic/python-docx/langgraph/pillow）与复现白名单（来自 `security.ALLOWED_REQUIREMENTS`）。`CRITICAL_REPRO_PACKAGES = {numpy, scipy, matplotlib}`（`preflight.py:45`）。只用 `importlib.util.find_spec` / `importlib.metadata`，**不 import 重包**（故缺包时 doctor 仍能跑）。`EnvironmentReport.fatal`（`preflight.py:84-92`）：Python 过低 或 缺编排依赖 或 缺关键复现库 → 致命。`format_report`（`preflight.py:170-200`，纯 ASCII 标记规避 GBK 控制台）、`environment_warning`（`preflight.py:203-221`，review 启动时 stderr 告警）。

### 4.10 openhands_repair.py（OpenHands 修复后端，325 行）

`run_openhands_repair_candidate`（`openhands_repair.py:45-125`）：在 `attempt_NN_openhands_candidate/` 副本里跑 agent，产出 diff（`build_candidate_diff`，`openhands_repair.py:252-268`），然后**用本地受限验收**（`validate_repro_project` + `run_repro_once`）判断是否 accepted（`openhands_repair.py:108`），accepted 才 `copy_candidate_changes` 回主项目。`invoke_openhands_sdk`（`openhands_repair.py:128-181`）：动态 import `openhands.sdk`，缺失则 RuntimeError（`case_obs` 实跑里就是这条：SDK 未装 → hybrid 回落 LLM）。模型/key/base 取 `GENG_OPENHANDS_*` 回落 `GENG_LLM_*`（`openhands_repair.py:80-82`）。`normalize_openhands_model`（`openhands_repair.py:184-189`，有 base 则前缀 `openai/`）。修复 prompt（`openhands_repair.py:192-249`）显式声明受限边界（禁网络/子进程/env/绝对路径/非白名单依赖）与验收检查项。

### 4.11 schema_models.py + schemas.py — 见 [§6](#6-schema-真源)

### 4.12 outputs.py（产物检查 / manifest 写入 / 项目校验，232 行）

`REQUIRED_REPRO_FILES`（`outputs.py:13-23`，9 个文件，注意**不含 `src/__init__.py`**——模板会额外写它但它不在必需集）。`write_file_manifest`（`outputs.py:36-65`）：写每个 file，`resolve_inside`（`outputs.py:94-108`，拒绝绝对路径/`..`/逃逸，剥 `repro_project/` 前缀），三选一内容字段（`_extract_manifest_content`，`outputs.py:68-91`）。`validate_repro_project`（`outputs.py:111-138`）：必要文件齐全 + `py_compile` 全编译，跳过 `__pycache__`/`repair_logs`。`inspect_output_artifacts`（`outputs.py:149-193`）：新鲜度（`since` mtime）+ 有效性（`_valid_csv` 至少 2 行有内容、`_valid_png` 校验 PNG 魔数 + IHDR、`_valid_summary_json` 必须含 task_id/tasks/metrics/results/assumptions 之一）。

### 4.13 prompts.py（PromptBook，32 行）

`PromptBook.render`（`prompts.py:22-32`）：`{{var}}` 占位符（`PLACEHOLDER_RE`，`prompts.py:9`），**严格**——缺变量抛 `KeyError`（`prompts.py:29-30`）。自动注入 `dependency_policy`（`prompts.py:24`，来自 `security.dependency_policy_prompt_text()`，按当前环境已装/未装动态生成白名单）。

### 4.14 facts_normalize.py / tasks_normalize.py（归一化 / 部分接受 / 截断抢救）— 见 [§10](#10-兜底体系与死命令哲学)

### 4.15 heuristic_fallbacks.py（关键词兜底，288 行）

`build_fallback_engineering_facts`（`heuristic_fallbacks.py:7-114`）：关键词扫描信道（AWGN/Rayleigh/...）、调制（BPSK/QPSK/QAM/OFDM/MIMO）、指标（BER/SER/...）、SNR 值、figure，每条 confidence=low、附 `_meta.local_fallback_used`。`build_fallback_repro_tasks`（`heuristic_fallbacks.py:117-176`）：按检测到的 metric 生成单个 BER/SER/throughput/accuracy-vs-SNR 任务。`_guess_repro_type`（`heuristic_fallbacks.py:233-249`）按关键词猜复现类型。

### 4.16 template_project.py（确定性模板兜底，371 行）

`build_template_repro_project_manifest`（`template_project.py:30-86`）：返回完整 9+1 文件 manifest（含 `src/__init__.py`），`_meta.template_fallback_used=True`。`choose_template_name`（`template_project.py:13-27`）：BER/accuracy/generic 三模板。配置从 facts/tasks 抽调制、信道、SNR、num_bits（`_build_config`，`template_project.py:89-112`）。代码体是写死的字符串常量 `RUN_EXPERIMENT`/`CHANNEL_PY`/`MODULATION_PY`/`METRICS_PY`/`SIMULATION_PY`（`template_project.py:232-371`），用 numpy + matplotlib + 解析式 BER 近似 + 二项采样产出 results.csv/ber_comparison.png/summary.json。

### 4.17 experiment_index.py（本地实验索引，227 行）

`build_local_experiment_index`（`experiment_index.py:7-60`）：**不调 LLM**，把每个 task 映射成 experiment，回指 facts 的 source.page/chunk_id + 论文块关键词匹配补页（`_sources_for_experiment`，`experiment_index.py:125-144`），缺项写入 `limitations` 并将 status 设 `ready_with_limitations`（`experiment_index.py:48`）。

### 4.18 status.py（断点续跑状态，178 行）

`inspect_case_status`（`status.py:40-64`）：按 `STAGES`（`status.py:11-23`）逐阶段 `inspect_stage`（`status.py:67-108`，对有 schema 的阶段做 `validate_stage`），算 `next_stage`、`resume_from`（`RESUME_LABELS`，`status.py:25-37`）、`suggested_command`。被 CLI `status` 和 Web 进度共用。

### 4.19 docx_writer.py（Word 报告，538 行）

`write_review_docx`（`docx_writer.py:39-231`，主审查报告）、`write_result_review_docx`（`docx_writer.py:234-311`，结果审查报告）。用 python-docx，中文字体 Microsoft YaHei/SimSun，统一表格样式，底部固定免责声明 `DISCLAIMER`（`docx_writer.py:22`）。`pipeline._generate_docx_reports`（`pipeline.py:1049-1123`）调用，失败写 `docx_generation_error.json` 而不中断主流程。

### 4.20 verdict.py（最终复现结论，217 行）

`derive_reproducibility_verdict`（`verdict.py:16-137`）：**纯本地决策树**，无 LLM。优先级：runtime 失败 → `failed_to_reproduce`（`verdict.py:32-39`）；无 result_review → `inconclusive`；result_review 失败 → `inconclusive`；否则按 `overall_alignment` × `overall_result_credibility` × `risk_level` 映射到 6 档结论（`ReproducibilityVerdict` 枚举）。`_limited_by_template`（`verdict.py:151-161`）：用过模板兜底则把 `fully/mostly_reproduced` 降级为 `partially_reproduced`。

### 4.21 runner_types.py / json_utils.py / export_schemas.py

- `runner_types.py`（19 行）：`RepairBackend` 枚举（llm/hybrid/openhands）+ `normalize_repair_backend`。
- `json_utils.py`（101 行）：`pretty_json`、`parse_json_object`（`json_utils.py:21-33`，剥 `<think>` 块 + 代码围栏；`allow_loose_manifest` 时走 `_parse_json_object_fallback` → `parse_loose_file_manifest` 正则抢救 file manifest，`json_utils.py:58-86`）。`prepare_json_candidate`（`json_utils.py:36-51`）公开供截断抢救复用。
- `export_schemas.py`（21 行）：`python -m geng_agent.export_schemas --out schemas` 把 Pydantic 模型导出为 JSON Schema。

### 4.22 web/（极简 Web）

- `app.py`（106 行）：FastAPI，`GET /`、`/api/health`、`POST /api/runs`（上传 PDF 或 PDF URL）、`GET /api/runs/{id}`、`/api/runs/{id}/stream`（SSE 进度）。
- `jobs.py`（221 行）：单并发后台线程跑 `ReviewPipeline.run(run_repro=True, result_review=True, resume=False, template_fallback=True)`（`jobs.py:142-149`）；`_enqueue` 拒绝并发（`jobs.py:117-118`）；PDF 上限 80MB（`jobs.py:19`）。
- `stages.py`（49 行）：把内部阶段映射成 9 个面向用户的展示阶段。
- `__main__.py`（19 行）：uvicorn 启动，默认 `127.0.0.1:8765`。
- Web 不支持异构代码审查、不暴露 `--code-review`（`jobs.py:141` 直接 `ReviewPipeline(client=client)`，无 code_review_client）。

---

## 5. 数据与产物（case 目录布局）

下表基于 `case_obs/` 实例 + 代码写盘点。完整布局：

```
case_001/
├─ paper_chunks.json            # load_paper 输出：source_path/format/chunk_count/chunks[{chunk_id,page,section,text}]
├─ engineering_facts.json       # ① 工程事实 + missing_information + _meta（归一化/丢弃记录）
├─ repro_tasks.json             # ② 复现任务数组 + _meta
├─ experiment_index.json        # ②b 本地实验索引 experiments[] + _meta
├─ repro_project_manifest.json  # ③ 生成的文件清单（files[] + _meta，含 chunked_generation_used / project_plan）
├─ runtime_result.json          # ③运行：enabled/passed/run_profile/completed_profiles/repair_backend/
│                               #        repair_attempts_used/attempts[]/phase_results[]/repair_failures[]/
│                               #        openhands_attempts[]/artifacts/logs_dir/security_issues/requirements_issues
├─ runtime_result_pre_fallback.json  # 仅当生成项目运行失败后退模板时保留（pipeline.py:340）
├─ code_review.json             # ③.5 整项目忠实度审查（--code-review 时）：enabled/passed/revised/reviews[]/unresolved_findings
├─ result_review.json           # ④ 结果级审查：overall_*/experiment_reviews[]/cross_experiment_findings/recommended_human_checks/note
├─ result_review.md             # ④ 可读版
├─ result_review_error.json     # ④ 仅失败时（pipeline.py:1046）
├─ risk_report.json             # 风险汇总：risk_level/risk_dimensions/findings/scientific_check/result_review/
│                               #          experiment_index/code_review/reproducibility_verdict/docx_generation
├─ generated_files.json         # 汇总快照：files/validation/runtime_result/scientific_check/experiment_index/
│                               #          manifest_meta/code_review/result_review/reproducibility_verdict/docx_generation
├─ review.md                    # 主报告（Markdown）
├─ review.docx / result_review.docx   # Word 报告
├─ docx_generation_error.json   # 仅 docx 生成失败时
├─ audit/                       # 每阶段提示词 + 原始输出 + 校验记录（见下）
└─ repro_project/
   ├─ README.md requirements.txt config.json config_smoke.json run_experiment.py
   ├─ src/{__init__.py,channel.py,modulation.py,metrics.py,simulation.py}
   ├─ outputs/{results.csv, *.png, summary.json}
   └─ repair_logs/             # 返修留痕（attempt_NN_*、smoke_/full_ 前缀、openhands_*）
```

`audit/` 内文件命名规律（来自 `pipeline._call_validated_json` 等）：

| 文件名模式 | 含义 |
|---|---|
| `<stage_label>.md` | 该阶段渲染后的完整提示词 |
| `raw_<stage_label>_attempt_N.txt` / `raw_<stage_label>.txt` | LLM 原始输出（每次尝试 + 最新一次） |
| `validation_<stage_label>_attempt_N.json` | 该次尝试的校验结果（ok + errors） |
| `llm_error_<stage_label>_attempt_N.json` | LLM 请求异常记录 |
| `partial_<file_label>.json` | 逐文件生成进度 |
| `local_fallback_<stage_label>.json` / `template_fallback_03_*.json` | 兜底触发记录 |
| `resume_used_/resume_invalid_/resume_recovered_*.json` | 断点续跑命中/失效/恢复记录 |
| `03a_generate_repro_project_plan.json` / `03_generate_repro_project_chunked_manifest.json` | 计划与拼装后的 manifest |
| `04a_review_reproduction_experiment_NN_*.{md,txt,json}` / `04b_aggregate_reproduction_results.json` | 逐实验结果审查 + 聚合 |
| `code_review_NN.json` / `raw_code_review_revise_NN_*.txt` | 忠实度审查与返修 |

> 重要：`case_obs/` 里还有 `OBSERVER_LOG.md`、`reflections/`（step_*.json、final_reflection.json）、`doctor_console.log`、`run_console.log`。这些**不是 geng_agent 本体产物**——它们来自外部观察/记录脚本（reflections 本是已删除监督层的产物形态）。本体 `ReviewPipeline.run` 不写 `reflections/`。

### 5.1 case_obs 实例的真实教训

该实例论文是 LEO 卫星 MU-MIMO（`paper_repro_type: mimo_ofdm`），任务是复现 Fig.4/5/7 的和速率 CDF。`runtime_result.json` 显示：smoke 相 LLM 修复一次（CSV fieldnames 不匹配）通过、OpenHands 因 SDK 未装失败、hybrid 回落 LLM；full 相超时后重试通过。但 `summary.json` 与 `result_review.json` 揭示该 run **最终落到了 template 兜底**（generic BER/SNR 模板），产物是 BER 曲线而非论文的和速率 CDF，于是 `result_review` 正确判定 `overall_alignment: mismatch`、`overall_result_credibility: low`。这是[§10](#10-兜底体系与死命令哲学)"一篇论文吃兜底=失败"的活样本。

---

## 6. Schema 真源

**唯一真源是 Pydantic 模型** `geng_agent/schema_models.py`（369 行）。导出 JSON Schema 到 `schemas/`（`export_json_schemas`，`schema_models.py:358-369`）。

### 6.1 模型注册表（`SCHEMA_MODELS`，`schema_models.py:307-320`）

| stage 键 | 模型类 | 用途 |
|---|---|---|
| `engineering_facts` | `EngineeringFactsDocument` (`:106`) | ① |
| `repro_tasks` | `ReproTasksDocument` (`:153`，`min_length=1`) | ② |
| `experiment_index` | `ExperimentIndexDocument` (`:170`) | ②b |
| `repro_project_manifest` | `ReproProjectManifest` (`:214`) | ③ 拼装 manifest |
| `repro_project_plan` | `ReproProjectPlan` (`:224`) | ③a 计划 |
| `repro_project_file` | `ReproProjectFile` (`:230`) | ③b 单文件 |
| `repair_manifest` | `RepairManifest` (`:242`) | 运行/审查修复 |
| `result_review_experiment` | `ExperimentResultReview` (`:249`) | ④ 逐实验 |
| `result_review` | `ResultReviewDocument` (`:262`) | ④ 聚合 |
| `supervisor_decision` | `SupervisorDecisionDocument` (`:271`) | **休眠/陈旧**（监督层已删，见[§13](#13-当前状态与近期重大变更)） |
| `reproducibility_verdict` | `ReproducibilityVerdictDocument` (`:282`) | 最终结论（本地生成 + schema 自检） |
| `code_faithfulness_review` | `CodeFaithfulnessReviewDocument` (`:300`) | ③.5 忠实度审查 |

所有模型继承 `StrictModel`（`schema_models.py:80-81`，`extra="forbid"`）。关键枚举（`Literal`）：`PaperReproType`(`:17`)、`FactType`(`:29`)、`MetricName`(`:47`)、`ResultCredibility`/`ResultAlignment`(`:62-63`)、`ReproducibilityVerdict`(`:70`) 等。`ManifestFile` 是三选一联合类型（`ManifestTextFile`/`ManifestLinesFile`/`ManifestBase64File`，`schema_models.py:174-211`），base64 还校验解码为 UTF-8 且 ≤ 2MB（`schema_models.py:195-208`）。

### 6.2 校验流程（schemas.py，183 行）

`validate_stage(stage, data)`（`schemas.py:25-40`）：`model_validate`（**剥除 `_meta` 再校验**，`schemas.py:31`，因为 `_meta` 是本地附加的风险元数据，不属公开契约）+ 业务规则（manifest 类做 `validate_manifest_business_rules`，`schemas.py:91-137`：每 file 恰好一个内容字段、路径去重/不逃逸、必要文件齐全、repair 的 touched_files 必须出现在 files）。

额外的跨工件校验（pipeline 作为 `extra_validation` 传入）：
- `validate_fact_sources`（`schemas.py:47-61`）：facts 的 `source.chunk_id` 必须属于 paper_chunks。
- `validate_task_fact_refs`（`schemas.py:64-88`）：task 的 `required_facts` 必须能对应到已抽取 fact 的 (type,name)。

`response_format_for_stage`（`schema_models.py:346-355`）：把模型转成 OpenAI `json_schema` response_format（`strict=True`）发给兼容模型；服务端不支持时由 `llm.py` 回退 `json_object`（仅纯文本路径）。

> `schemas/` 目录里有 `supervisor_decision.schema.json`（导出自仍注册的 `supervisor_decision`）——陈旧产物。

---

## 7. 提示词

`geng_agent/prompts/` 共 11 个 `.md`。**被代码实际渲染的有 9 个**；两个是陈旧/未用：

| 提示词 | 渲染处 | 状态 |
|---|---|---|
| `extract_engineering_facts.md` | `pipeline.py:194` | ① 用 |
| `build_repro_tasks.md` | `pipeline.py:226` | ② 用 |
| `generate_repro_project_plan.md` | `pipeline.py:684` | ③a 用 |
| `generate_repro_project_file.md` | `pipeline.py:714` | ③b 用 |
| `review_repro_project_code.md` | `code_review.py:139,265` | ③.5 + 单文件审查 用 |
| `repair_repro_project_for_review.md` | `code_review.py:194` | ③.5 返修 用 |
| `repair_repro_project.md` | `runner.py:181` | 运行修复 用 |
| `review_reproduction_experiment.md` | `result_review.py:155` | ④ 逐实验 用 |
| `review_reproduction_results.md` | `result_review.py:69` | ④ overview，**仅写 audit，不发 LLM** |
| `build_experiment_index.md` | — | **未用**（②b 改本地 `build_local_experiment_index`） |
| `generate_repro_project.md` | — | **未用**（③ 改分块计划+逐文件） |

### 7.1 关键约束逐条

**数值稳健性（第 10 条）**——同时出现在 `generate_repro_project_file.md:38-45` 与 `repair_repro_project.md:21-25`、`generate_repro_project.md:44-47`：

- 概率/误码率类指标（BER/SER/BLER/outage）物理上必须落 [0,1]，写 CSV 或续用前裁剪；取对数前用极小正数下界（1e-12）替 0/负值，避免 log(0)/log(负)。
- `np.log/np.sqrt` 参数先 `max(x, ε)`；除法分母先保证非零；`np.polyfit/mean/max`/拟合前判空，空则跳过写 NaN/哨兵，**绝不抛异常**。
- 易抖运算（Gauss-Chebyshev 求积、特征函数、渐近展开）用数值稳定写法，避免灾难性相消。

**非致命执行（第 11 条）**——`generate_repro_project_file.md:42-44`、`generate_repro_project.md:48-50`、`repair_repro_project.md:23`：

- `run_experiment.py` 必须把每个实验/曲线独立包 try/except，单实验异常写入 summary.json 对应条目并继续；只要至少一个实验产出有效结果，脚本就以退出码 0 结束。

**页面图多模态说明**：`extract_engineering_facts.md:17`（结合页面图读系统/框图、星座图、坐标轴/图例、只画在图里的数值与趋势，来源仍填该页 chunk_id/page）；`generate_repro_project_plan.md:33`（参考目标图曲线条数/坐标范围/baseline/趋势）；`generate_repro_project_file.md:45`（产物与论文图对照，**结果仍须由仿真算出，不得照抄图中数值**）；`review_reproduction_*.md`（图像按 UNTRUSTED DATA、读不准点须写 limitations）。

**行/字符上限**：`generate_repro_project_plan.md:31`（全项目 ≤1000 行、单文件 ≤200 行、README/requirements/JSON 各 ≤200 行）；`generate_repro_project_file.md:54`（单文件 ≤200 行、README/config/Python ≤20000 字符、requirements ≤4000 字符）。与 `REPRO_PROJECT_FILE_LIMITS`（`pipeline.py:1247-1257`）一致。

**`review_feedback` 槽位**：`generate_repro_project_file.md:79-80`——回灌上一轮单文件忠实度审查的 blocking 问题，要求逐条修复、不得为绕审查删任务或硬编码结果。pipeline 通过 `review_feedback_json` 注入（`pipeline.py:764`），默认空串。

**忠实度审查双证据约束**：`review_repro_project_code.md:12`——"每条 finding 必须同时给出 evidence_spec 与 evidence_code，给不出双证据的发现一律不要输出"，与本地 `filter_grounded_findings`（`code_review.py:75-93`）双向呼应。

**结果审查中文约束**：`review_reproduction_experiment.md:11`、`review_reproduction_results.md:11`——所有自然语言字段必须中文（公式/变量名/文件名/task_id 可留原文）。

**依赖策略注入**：所有生成/修复类提示词含 `{{dependency_policy}}`（如 `generate_repro_project_file.md:12`），由 `security.dependency_policy_prompt_text()` 按**当前环境已装库**动态生成"已装可用 / 白名单但未装默认别用"两段（`security.py:173-197`），并强调"禁用 broad try/except 静默降级"（`security.py:190`）。

---

## 8. 安全模型

论文/日志/代码/图像一律按 `UNTRUSTED DATA` 处理（`wrap_untrusted` 统一包裹；系统消息 `pipeline.py:39-45` 重申不可覆盖系统规则、不可当指令执行）。

### 8.1 依赖白名单

`ALLOWED_REQUIREMENTS = {numpy, scipy, matplotlib, scikit-learn, sklearn, reedsolo, pillow}`（`security.py:12-20`）。这是**唯一真源**；`pyproject.toml [repro]`（`pyproject.toml:30-36`）由 `tests/test_preflight.py` 锁死不漂移（`pyproject.toml:26-29`）。`validate_requirements`（`security.py:141-170`）：拒绝 `-`/URL/`@`/路径语法，包名归一化后必须在白名单，且必须在当前环境已装（`importlib.util.find_spec`）。`validate_import_requirements`（`security.py:200-243`）：每个第三方 import 必须在 requirements.txt 声明且已装，且**禁止 broad try/except 包住第三方 import**（`security.py:229-242`，防静默科学降级）。

### 8.2 禁用 import / builtin / call

- `FORBIDDEN_IMPORTS`（`security.py:33-45`）：socket/requests/urllib/http/ftplib/paramiko/subprocess/multiprocessing/webbrowser/ctypes/**importlib**。
- `FORBIDDEN_BUILTINS`（`security.py:51-61`）：eval/exec/compile/`__import__`/getattr/setattr/delattr/globals/vars——封堵基于字符串拼接绕过静态名扫描的反射路径（注释 `security.py:47-50`）。
- `FORBIDDEN_CALLS`（`security.py:63-80`）：os.system/popen/spawn*/remove/unlink/rmdir、shutil.rmtree/move、subprocess.run/call/Popen、Path.home/expanduser。
- 另：`os.environ`/`os.getenv` 属性访问被禁（`security.py:351-354`），`open`/`Path`/`PurePath` 的**绝对路径字面量**被禁（`_check_absolute_path_literal`，`security.py:367-375`）。

`static_scan_repro_project`（`security.py:323-355`）用 AST 遍历每个项目 .py（跳过 `__pycache__`/`repair_logs`/`outputs`），语法错误也作为 issue 上报。

### 8.3 安全环境 / 脱敏

`build_safe_env`（`security.py:105-121`）：**白名单式**只保留 PATH/SystemRoot/WINDIR/TEMP/TMP/PYTHONIOENCODING/MPLBACKEND，强制 `MPLBACKEND=Agg`、`PYTHONIOENCODING=utf-8`，并 pop 掉所有 `SENSITIVE_ENV_KEYS`（各家 API key + 代理，`security.py:82-96`）。`redact_text`（`security.py:124-128`）按 `SECRET_PATTERNS`（sk-、Bearer、api_key/token/secret/password=...，`security.py:98-102`）脱敏；`redact_data` 递归脱敏（`security.py:131-138`）。运行日志写盘前一律过 `redact_data`/`redact_text`（`runner.py:138/235/362-363`）。

### 8.4 多层语法/编译/内容类型校验

| 层 | 函数 | 时机 |
|---|---|---|
| 单文件 ast/json 内容类型 | `_content_type_issues`（`pipeline.py:1316-1342`） | ③b 生成每个文件后 |
| 项目级 `python_compiles`（py_compile） | `validate_repro_project`（`outputs.py:111-138`） | ③ 写盘后 / 运行前 |
| AST 静态安全扫描 | `static_scan_repro_project`（`security.py:323-355`） | 每次 `run_repro_once` 前 |
| 逐文件 ast 编译（import 校验内） | `validate_import_requirements`（`security.py:200-243`） | 每次运行前 |

任何安全/依赖 issue → `run_repro_once` 直接 `blocked_by_security=True` 拒跑（`runner.py:309-331`），不会真正执行 LLM 代码。

---

## 9. 多模态能力

近期新增的多模态贯穿三处：

1. **页面渲染** `render_pdf_pages_for_llm`（`result_review.py:708-732`）：fitz 渲染（`fitz.Matrix(1.5,1.5)`），`encode_png_bytes_for_llm`（`result_review.py:735-746`）转 RGBA 缩略到 1600。`encode_png_for_llm`（`result_review.py:637-659`）处理本地输出 PNG。
2. **第①轮多模态抽事实 + 第③a 计划喂图**：`_render_paper_images`（`pipeline.py:881-897`）渲染**全部页**（`max_pages=60`），经 `_complete_maybe_multimodal`（`pipeline.py:899-912`）喂给 ① facts（`pipeline.py:209`）与 ③a plan（`pipeline.py:700`）。**注意逐文件 03b 不喂图**（`pipeline.py:733` 注释明说"Page images go to the plan only"）。
3. **第③b 逐文件忠实度审查**：`review_single_generated_file`（`code_review.py:239-281`）——纯文本（喂代码内容，不喂图）。
4. **第④轮结果级多模态审查**：`complete_multimodal` 逐实验喂"本地输出 PNG + 论文页 PNG"（`result_review.py:167-172`）。

**多模态的健壮性边界**：
- 客户端无 `complete_multimodal` 或非 PDF → `_render_paper_images` 返回 `[]`，①/③a 透明退纯文本（`pipeline.py:887-889`）。
- ④ 结果审查**不退纯文本**：无有效 PNG 直接 RuntimeError（`result_review.py:66-67`），不支持多模态的模型会写 `result_review_error.json`（`pipeline.py:1039-1047`），但前面各阶段照常完成。
- `complete_multimodal` 关闭 response_format 回退（`llm.py:117`），即多模态严格要求 json_schema。

---

## 10. 兜底体系与"死命令"哲学

### 10.1 兜底路径全表

| 触发条件 | 兜底动作 | 代码 |
|---|---|---|
| ① facts schema/校验失败且**零**可用事实 | 关键词兜底 `build_fallback_engineering_facts` | `pipeline.py:216-223` |
| ① facts 枚举近义/空字段/多余键 | 本地归一化（保留真实抽取） | `finalize_engineering_facts`（`facts_normalize.py:347-371`） |
| ① facts 单条不可修复/无 chunk_id | 部分接受（只丢该条） | `select_valid_engineering_facts`（`facts_normalize.py:326-344`） |
| ① facts 输出被截断 | 截断抢救（salvage 数组前缀） | `recover_truncated_engineering_facts`（`facts_normalize.py:427-452`） |
| ② tasks 同上四种 | 归一化/部分接受/截断抢救/关键词兜底 | `tasks_normalize.py` + `pipeline.py:246-254` |
| ③ 生成项目缺必要文件 或 不可编译 | 确定性模板兜底 | `pipeline.py:287-296` |
| ③ 分块生成抛异常 | 模板兜底 | `pipeline.py:651-666` |
| ③b 单文件不是合法 .py/.json | 该文件重生成（避免整项目退模板） | `_content_type_issues`（`pipeline.py:1316-1342`） |
| ③运行失败 + 返修耗尽 + **无**部分产物 + 兜底开 | 退模板 + 重跑 | `pipeline.py:337-361` |
| ③运行失败但**有**有效 CSV | 保留生成项目 + 标 partial_success（不退模板） | `_assess_partial_success`（`pipeline.py:328-336, 1410-1428`） |
| manifest 宽松恢复 | 标 `loose_recovery_used`（人工抽查） | `json_utils.py:58-86` + `pipeline.py:1832-1833` |

所有兜底都写 `_meta` 并在 `risk_report.findings` 标注（`build_risk_report`，`pipeline.py:1832-1879`：`template_fallback_used`/`local_stage_fallback_used`/`facts_truncation_recovered`/`facts_partial_acceptance_used`/`facts_normalized` 等）。`derive_reproducibility_verdict` 对用过模板的把正面结论降级（`verdict.py:151-161`）。

### 10.2 归一化的"传统功夫"细节

`facts_normalize.py` 与 `tasks_normalize.py` 共三层（docstring `facts_normalize.py:1-21`、`tasks_normalize.py:1-21`）：归一化（枚举近义词表，如 `FACT_TYPE_SYNONYMS` `facts_normalize.py:55-99`、`METRIC_SYNONYMS` `tasks_normalize.py:58-77`）→ 部分接受 → 截断抢救（`_salvage_array_objects` 手写括号深度扫描，`facts_normalize.py:387-424`）。tasks 还有一套精巧的 `required_facts` 别名解析（`_fact_aliases`/`_ref_aliases`，`tasks_normalize.py:136-194, 364-378`），把模型写的 `f_ch_awgn`、`fig_4`、`snr_10db` 等回指到真实事实。`MIN_ENGINEERING_FACTS=1`（`facts_normalize.py:45`）：只要有一条有出处的事实就不退关键词兜底。

### 10.3 "死命令"哲学

README/系统设计的核心立场：**兜底是安全网，不是目标**。一篇论文最终吃了 template 兜底，等价于"复现失败"——产物是 generic 模板曲线、与论文无关（`case_obs` 即典型，见[§5.1](#51-case_obs-实例的真实教训)）。正确做法是**从每次兜底中提炼经验、改进本体（提示词/归一化/校验/数值稳健性约束）以降低未来兜底率**，而不是把兜底当成"跑通了"。这一立场体现在：

- 提示词把"数值越界""单实验崩溃"显式标注为"最常见兜底来源"（`generate_repro_project_file.md:38,42`）。
- `risk_report` 把每条兜底都标成 finding，`verdict` 对模板兜底降级（不让兜底冒充复现成功）。
- smoke 通过的免责声明反复出现（不等于完整复现，`pipeline.py:1903`）。

---

## 11. 模型与配置

### 11.1 环境变量

| 变量 | 作用 | 读取处 |
|---|---|---|
| `GENG_LLM_API_KEY` / `GENG_LLM_BASE_URL` / `GENG_LLM_MODEL` | 主模型（生成/抽取/运行修复/结果审查） | `config.build_llm_client`（`config.py:54-56`） |
| `GENG_CODE_REVIEW_MODEL` / `_API_KEY` / `_BASE_URL` | 异构代码审查者；未设回落主模型 | `config.build_code_review_client`（`config.py:90-99`） |
| `GENG_OPENHANDS_MODEL` / `_API_KEY` / `_BASE_URL` | OpenHands 修复；回落 `GENG_LLM_*` | `openhands_repair.py:80-82` |
| `GENG_CASES_ROOT` | Web 案例根目录（默认 `~/Documents/geng_cases`） | `config.get_cases_root`（`config.py:37-41`） |

Windows 下 `get_config_value` 还查注册表（`config.py:14-34`），故 `setx` 设的变量也能读到。结果级审查要求模型支持 OpenAI 兼容多模态 `image_url`（否则写 `result_review_error.json`）。

### 11.2 解释器要求

复现代码需 numpy/scipy/matplotlib，而 harness 自带 venv 通常没有 → 项目固定用完整解释器 `D:\python\python.exe`（Python 3.13）。启动器 `run.ps1`（顶部 `$GengPython = 'D:\python\python.exe'`，`run.ps1:17`）与 `run.cmd`（`set "GENG_PYTHON=D:\python\python.exe"`，`run.cmd:9`）会切到项目目录并用该解释器调 `geng_agent`。**运行器用 `sys.executable`**（`runner.py:435-437`），所以跑流水线的解释器必须就是那个完整解释器。换机只需改这一行 + 装 `[repro]`。

### 11.3 安装组（pyproject.toml）

- 主依赖：pypdf / pydantic / pillow / pymupdf / python-docx / langgraph（`pyproject.toml:7-14`）。
- `[repro]`：numpy/scipy/matplotlib/scikit-learn/reedsolo（`pyproject.toml:30-36`）。
- `[openhands]`：openhands-sdk/tools（`pyproject.toml:17-20`）。
- `[web]`：fastapi/uvicorn/python-multipart（`pyproject.toml:21-25`）。
- 一条命令装齐运行 + 复现白名单：`<python> -m pip install -e ".[repro]"`。

### 11.4 doctor

`geng-agent doctor`（`cli.py:73-78`）输出实际解释器路径、Python 版本是否达标、逐库装没装；致命缺失退出码 1。`review` 启动也做一次轻量自检并 stderr 告警（`cli.py:125-136`）。

---

## 12. 测试

测试套件位于 `tests/`，共 **134 个测试**，全部通过（`D:\python\python.exe -m unittest discover -s tests` → `Ran 134 tests in ~17s, OK`，已实跑确认）。

| 测试文件 | 用例数 | 覆盖 |
|---|---|---|
| `test_pipeline.py` | 23 | 端到端编排、分块生成、单文件校验、partial_success、manifest 归一化、run_stage 重生成报告 |
| `test_runner.py` | 15 | 受限运行、smoke/full 两相、返修循环、安全拒跑、命令选择、OpenHands 桩 |
| `test_facts_normalize.py` | 14 | 枚举归一化、部分接受、截断抢救、floor |
| `test_code_review.py` | 10 | 双证据 grounding、整项目/单文件审查、返修、不阻断 |
| `test_multimodal_extraction.py` | 8 | 页面渲染、①/③a 喂图、非 PDF/非多模态回退 |
| `test_preflight.py` | 8 | doctor、白名单与 `[repro]` 同步锁定、fatal 判定 |
| `test_schemas.py` | 7 | 各 stage schema、跨工件校验、manifest 业务规则 |
| `test_security.py` | 7 | 白名单、禁用 import/builtin/call、脱敏、安全环境 |
| `test_result_review.py` | 7 | CSV/summary 摘要、选页/选图、聚合、最弱标签 |
| `test_outputs.py` | 6 | manifest 写入、resolve_inside、产物有效性、project 校验 |
| `test_tasks_normalize.py` | 5 | 任务归一化、required_facts 别名解析、截断抢救 |
| `test_verdict.py` | 5 | 结论决策树、模板降级 |
| `test_template_project.py` | 4 | 模板选择、配置抽取、确定性产物 |
| `test_json_utils.py` / `test_experiment_index.py` | 3 / 3 | JSON 解析/宽松恢复；本地实验索引 |
| `test_prompts.py` | 3 | 占位符严格渲染、缺变量报错、dependency_policy 注入 |
| `test_documents.py` / `test_docx_writer.py` | 2 / 2 | 分块（fitz/pypdf）；Word 生成 |
| `test_status.py` / `test_web_stages.py` | 1 / 1 | 断点续跑状态；Web 阶段映射 |

> `test_pipeline.py` 用 fake client（按 schema 名返回桩 JSON）驱动整条流水线，不触网。

---

## 13. 当前状态与近期重大变更

逐条对照需求清单核实（基于真实代码）：

**(a) 已移除运行期科学性闸门 `scientific_integrity`**：✔ 确认。全代码库无 `scientific_integrity` 标识符。残留的是 `build_scientific_check`（`pipeline.py:1759-1785`）——它只生成诊断 `scientific_check`（缺 baseline/空趋势/generic metric_value/公式未名指标），写进 risk_report，**不闸门、不阻断**（无任何 raise / 中止）。`case_obs` 即有 `scientific_check.ok=false` 但 run 照常完成（`generated_files.json:642-658`）。

**(b) 已移除监督层（supervise 命令 + supervisor.py）**：✔ 确认。无 `supervise` 子命令（`cli.py` 只有 review/status/doctor），无 `geng_agent/supervisor.py`。**残留物**：
- `schema_models.py:64,271-279,317,333` 仍注册 `SupervisorDecisionDocument` + `supervisor_decision`（导出了 `schemas/supervisor_decision.schema.json`）——无人引用。
- `pipeline.run_stage`（`pipeline.py:87-145`）+ `_apply_prompt_adjustment`（`pipeline.py:63-76`）+ `prompt_adjustments` 链——为监督层留的休眠 hook，仅测试用到 `run_stage`。
- `tools/make_harness_docx.py`（多处，如 `:90,93,265,296,550`）与 `docs/geng_agent_web_ui_design.md`（大量，如 `:15,17,34,62,75,93,129,557,749`）仍描述 supervise/supervisor.py/run_supervised_review/SuperviseOptions/`--max-supervisor-steps` 等——**陈旧文档**。
- `case_obs/reflections/`（step_*/final_reflection）是监督层产物形态的遗留样本。

**(c) PDF 切块器改用 PyMuPDF/fitz**：✔ 确认。`documents._load_pdf`（`documents.py:52-59`）fitz 优先、pypdf 兜底。

**(d) 新增多模态抽事实 + 计划喂图 + 逐文件审查**：✔ 确认。见[§9](#9-多模态能力)。

**(e) 新增逐文件 ast/json 内容类型校验**：✔ 确认。`_content_type_issues`（`pipeline.py:1316-1342`）。

**(f) 代码审查默认 5 轮返修 + 审查超时 1200s**：✔ 确认。`--code-review-attempts` 默认 5（`cli.py:64`），`--code-review-timeout` 默认 1200.0（`cli.py:66`）。

**(g) 单文件行数上限提到 200**：✔ 确认。`REPRO_PROJECT_FILE_LIMITS` 全部 `"lines": 200`（`pipeline.py:1247-1257`），提示词同步（`generate_repro_project_file.md:54`）。

**额外发现的现状细节**：
- `langgraph` 被声明为主依赖并在 doctor 检查，但**流水线未用 LangGraph**（手写编排），全代码无 `import langgraph`（仅 `preflight.py:40` 字符串）。
- `review_reproduction_results.md`（整体审查）只渲染进 audit，真正审查走逐实验版——整体 overview 的 LLM 调用是死代码路径。
- `generate_repro_project.md`（单体生成）与 `build_experiment_index.md`（LLM 实验索引）两个提示词已被分块生成/本地构建取代，**未被任何代码渲染**。
- `cli._add_common_review_args(include_resume=...)` 的 `True` 分支从未被调用（休眠）。

---

## 14. 已知局限 / 风险 / 改进方向

### 14.1 设计层面的固有局限

1. **忠实度审查不是正确性证明**（`code_review.py:19-21`）：受限于抽取 spec 的质量，无法证明数值与论文一致（那是运行/多模态层的事）。
2. **结果审查不读精确数值点**：依赖多模态模型读图，读不准只能写 limitations；`overall_*` 取**最弱标签**聚合（`weakest_label`，`result_review.py:545-550`），偏保守。
3. **本地实验索引是启发式**：`build_local_experiment_index` 按关键词回指页/块，可能漏页或误配（`_meta.local_fallback_used=True` 标记）。
4. **tasks 别名解析靠手写规则表**（`tasks_normalize.py`）：通信术语覆盖广但不可能穷尽，新命名风格可能解析失败 → 丢 required_facts ref。
5. **模板兜底产物与论文常无关**：generic BER/SNR 曲线对非信号链论文（如 case_obs 的 MIMO 和速率）天然 mismatch，只有"能跑出格式正确产物"的价值。

### 14.2 工程/健壮性风险

6. **依赖宿主解释器**：必须用装了 numpy/scipy/matplotlib 的解释器（`sys.executable`），否则每篇 numpy 论文吃兜底；doctor 是唯一防线但需用户主动跑。
7. **OpenHands 默认不可用**：SDK 未装时 hybrid 静默回落 LLM（`case_obs` 实例即此）；用户可能不知道 OpenHands 路径其实没生效。
8. **单并发 Web**：`jobs._enqueue` 拒绝并发（`jobs.py:117-118`），不适合多用户；后台线程崩溃只记 record.error。
9. **full 相超时常见**：case_obs 里 full 相先超时再重试通过；`--run-timeout` 默认 120s 对真实规模实验偏小，依赖 smoke→full 分相 + 返修缓解。
10. **安全扫描是 AST 名级**：封堵了反射 builtin，但理论上仍有未覆盖的绕过面（如 numpy/scipy 内部能力）；定位是"降低风险"非"沙箱隔离"（无进程/文件系统级隔离，仅 env 白名单 + 静态扫描 + 受限命令）。

### 14.3 代码卫生 / 改进方向

11. **清理监督层残留**：删 `supervisor_decision`（schema_models + schemas/ 导出）、`run_stage`/`_apply_prompt_adjustment`/`prompt_adjustments`、`include_resume` 死分支，或在文档中明确标注为保留接缝。
12. **更新陈旧文档**：`docs/geng_agent_web_ui_design.md` 与 `tools/make_harness_docx.py` 仍大篇幅描述 supervise，会误导新接手者。
13. **删除或归位未用提示词**：`generate_repro_project.md`、`build_experiment_index.md`、以及 `review_reproduction_results.md` 的整体 overview 分支。
14. **`langgraph` 依赖名实不符**：要么真的用上、要么从主依赖移除以减小安装面（它仍被 doctor 当编排关键依赖检查，缺它会判 fatal）。
15. **从兜底日志做闭环**：审计目录已完整记录每次兜底原因（`template_fallback_*`、`partial_*`、`validation_*`），可据此系统性地反推提示词/校验改进点，落实[§10.3](#103-死命令哲学)的"降低兜底率"。

---

## 附录 A：模块清单与体量

`geng_agent/` 源码行数（含 web/）：

| 模块 | 行数 | 模块 | 行数 |
|---|---|---|---|
| pipeline.py | 2213 | facts_normalize.py | 452 |
| result_review.py | 837 | template_project.py | 371 |
| runner.py | 593 | schema_models.py | 369 |
| docx_writer.py | 538 | security.py | 374 |
| tasks_normalize.py | 459 | openhands_repair.py | 325 |
| heuristic_fallbacks.py | 288 | code_review.py | 281 |
| outputs.py | 232 | experiment_index.py | 227 |
| preflight.py | 221 | web/jobs.py | 221 |
| verdict.py | 217 | cli.py | 187 |
| schemas.py | 183 | status.py | 178 |
| documents.py | 175 | llm.py | 160 |
| config.py | 109 | web/app.py | 106 |
| json_utils.py | 101 | web/stages.py | 49 |
| prompts.py | 32 | export_schemas.py | 21 |
| runner_types.py | 19 | web/__main__.py | 19 |
| __main__.py / __init__.py / web/__init__.py | 5/3/2 | — | — |

合计约 9567 行。测试约 3165 行（134 用例）。提示词 11 个 `.md`（9 个在用）。schemas/ 12 个 JSON（含 1 个陈旧 supervisor_decision）。

## 附录 B：残留 / 休眠 / 陈旧清单

| 项 | 位置 | 类别 | 说明 |
|---|---|---|---|
| `supervisor_decision` / `SupervisorDecisionDocument` | `schema_models.py:64,271-279,317,333` + `schemas/supervisor_decision.schema.json` | 休眠（注册但无引用） | 监督层已删 |
| `run_stage` + `_apply_prompt_adjustment` + `prompt_adjustments` | `pipeline.py:63-76,87-145,104,164,208,241,515,529` | 休眠 hook | 仅 `tests/test_pipeline.py:285` 用 run_stage；prompt_adjustments 无调用方传入 |
| `_add_common_review_args(include_resume=True)` 分支 | `cli.py:51-55` | 死分支 | 从未以 True 调用 |
| `langgraph` | `pyproject.toml:13` + `preflight.py:40` | 名实不符 | 声明为编排依赖但流水线未用 LangGraph，无 `import langgraph` |
| `generate_repro_project.md` | `geng_agent/prompts/` | 未用提示词 | 被分块生成取代 |
| `build_experiment_index.md` | `geng_agent/prompts/` | 未用提示词 | 被本地 `build_local_experiment_index` 取代 |
| `review_reproduction_results.md` 整体 overview LLM 调用 | `result_review.py:69-76` | 半死路径 | 只写 audit，不发 LLM；实际走逐实验版 |
| `tools/make_harness_docx.py` supervise 描述 | `:90,93,265,296,550` | 陈旧文档生成器 | 仍画 supervise 表/层 |
| `docs/geng_agent_web_ui_design.md` | 多处（`:15,17,34,62,75,93,129,259,267-269,272,322,557,670-677,706,749`） | 陈旧设计文档 | 大篇幅描述已删的 supervise/supervisor.py/SuperviseOptions |
| `case_obs/reflections/`、`OBSERVER_LOG.md` | case_obs | 非本体产物 | 外部观察脚本 / 监督层产物遗留 |
