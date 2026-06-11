# 耿同学agent 项目架构报告（当前源码版）

> 生成日期：2026-06-10。仓库根目录：`C:\Users\84475\Documents\耿同学agent`。
>
> 本报告以当前源码为准，不沿用旧文档结论。上一版文档中关于独立代码审查路径和旧可选修复后端的描述已经过期；当前源码和 CLI 中没有这些功能路径。

## 1. 项目定位

耿同学agent 是一个本地运行的通信领域论文工程复现审查工具。它给定 PDF/TXT/Markdown 论文，生成可追溯的工程事实、复现任务、复现项目、运行结果、结果级审查和风险报告。

项目边界：

- 不直接判定论文造假，只输出复现风险、差异和证据链。
- LLM 负责抽取、设计、生成、修复和结果级解释。
- 本地程序负责 schema 校验、路径约束、安全扫描、受限运行、产物检查、兜底和报告汇总。
- 论文文本、日志、代码片段、表格、图片都按不可信数据处理。

## 2. 入口与依赖

包信息在 `pyproject.toml`：

- `geng-agent = geng_agent.cli:main`
- `geng-agent-web = geng_agent.web.__main__:main`
- 基础依赖：`pypdf`、`pydantic`、`pillow`、`pymupdf`、`python-docx`
- 可选依赖：
  - `[repro]`：生成复现代码允许使用的科学计算库
  - `[web]`：FastAPI/uvicorn/python-multipart

CLI 命令来自 `geng_agent/cli.py`：

- `review`：运行完整审查流水线
- `status`：检查已有 case 目录的续跑状态
- `doctor`：自检 Python 版本和依赖

Web 服务来自 `geng_agent/web/__main__.py`，默认监听 `127.0.0.1:8765`。

## 3. API 接入

### 3.1 LLM API

LLM 客户端在 `geng_agent/llm.py`，实现 OpenAI-compatible Chat Completions：

- 文本接口：`complete(...)`
- 多模态接口：`complete_multimodal(...)`
- 请求地址：`{base_url}/chat/completions`
- 鉴权：`Authorization: Bearer <api_key>`
- 文本 JSON schema 不被服务端支持时，会从 `json_schema` 回退到 `json_object`
- 多模态结果审查不做纯文本回退

配置入口在 `geng_agent/config.py`：

| 客户端 | 环境变量 | 用途 |
|---|---|---|
| 主模型 | `GENG_LLM_API_KEY` / `GENG_LLM_BASE_URL` / `GENG_LLM_MODEL` | 事实抽取、任务设计、plan、运行修复、结果审查 |
| 第二事实抽取模型 | `GENG_LLM2_API_KEY` / `GENG_LLM2_BASE_URL` / `GENG_LLM2_MODEL` | 第一轮事实抽取 ensemble，未完整配置则关闭 |
| 专用生成模型 | `GENG_GEN_API_KEY` / `GENG_GEN_BASE_URL` / `GENG_GEN_MODEL` | 第三轮逐文件代码生成和 science repair，未配置则回退主模型 |

### 3.2 本地 Web API

当前 Web 是极简本地接口，不是旧设计稿里的完整 React/监督调度平台。

已实现接口在 `geng_agent/web/app.py`：

| 方法 | 路径 | 作用 |
|---|---|---|
| `GET` | `/` | 返回静态页面 `web/static/index.html` |
| `GET` | `/api/health` | 检查 LLM 配置、cases root、当前 active run |
| `POST` | `/api/runs` | 上传 PDF 或传 PDF URL，创建并启动 run |
| `GET` | `/api/runs/{run_id}` | 查询 run 状态和阶段进度 |
| `GET` | `/api/runs/{run_id}/stream` | SSE 推送阶段进度 |

Web job 实现在 `geng_agent/web/jobs.py`：

- 单 active run 模型，同时只允许一个 queued/running 任务。
- 上传 PDF 和 URL PDF 都会落盘到 `GENG_CASES_ROOT` 或默认 `~/Documents/geng_cases`。
- Web 调用 `ReviewPipeline(client=build_llm_client()).run(...)`。
- Web 当前不暴露 CLI 的全部高级参数，也没有独立设置页。

## 4. 当前主流水线

核心类是 `geng_agent/pipeline.py::ReviewPipeline`。构造参数：

- `client`：主 LLM 客户端
- `prompt_book`：提示词载入器
- `extraction_client_2`：可选第二事实抽取模型
- `generation_client`：可选专用代码生成模型

`ReviewPipeline.run(...)` 的当前阶段：

1. **读取论文**
   - PDF/TXT/Markdown 转成 `paper_chunks.json`
   - PDF 会渲染页面图像，供多模态事实抽取、plan 和结果审查使用

2. **第一轮：工程事实抽取**
   - 渲染 `prompts/extract_engineering_facts.md`
   - 调用主模型或主模型 + 第二模型 ensemble
   - 本地做 schema 校验、来源回指校验、事实归一化、截断抢救
   - 失败且允许兜底时，生成本地 fallback facts

3. **第一轮补强：facts gap finder**
   - 确定性检查图/表/figure claim 覆盖
   - 针对遗漏项补抽事实
   - 默认 `--facts-gap-rounds 3`

4. **可选论文思路闭环：paper thesis**
   - `--science-loop` 打开
   - 抽取中心主张、机制、方法排序等
   - 作为后续代码生成和结果审查的锚点

5. **第二轮：复现任务生成**
   - 渲染 `prompts/build_repro_tasks.md`
   - 调用 LLM 生成 `repro_tasks.json`
   - 本地校验 task 引用事实、指标、输出列、趋势、baseline

6. **第二轮补强：tasks gap finder**
   - 检查每个可复现实验是否有任务覆盖
   - 默认 `--tasks-gap-rounds 3`

7. **本地实验索引**
   - `experiment_index.py` 本地构建 `experiment_index.json`
   - 不调用 LLM

8. **第三轮：复现项目生成**
   - 先生成 `repro_project_plan`
   - 再逐文件生成 `repro_project_file`
   - 逐文件阶段可走 `generation_client`
   - `--per-task-layout` 打开后，LLM 只生成共享 `src/` 和每任务一个 `tasks/<module>.py`
   - `run_experiment.py`、`tasks_manifest.json`、`src/_io.py` 由 harness 本地注入

9. **本地项目校验与兜底**
   - 校验 manifest、路径、必要文件、语法
   - 自动 reconcile 白名单 requirements
   - 生成项目无法通过本地校验时，可写入 deterministic template fallback

10. **受限运行**
    - 默认不运行生成代码
    - 用户传 `--run-repro` 后进入 runner
    - runner 做依赖白名单、静态安全扫描、干净环境变量、输出新鲜度、CSV/PNG/summary 校验

11. **运行修复**
    - 运行失败时触发修复循环
    - LLM 生成 repair manifest
    - 候选修复必须通过本地受限验收才会写回主项目

12. **结果级多模态审查**
    - 仅在 `--run-repro` 且有可用输出时运行
    - template fallback 项目会跳过结果审查，避免把通用模板误评为论文复现
    - 按任务逐个调用多模态模型，失败的单个实验降级为 `cannot_assess`
    - 如果全部实验审查都失败，则整个 result review 失败

13. **Science repair**
    - 仅在 `--science-loop + --per-task-layout` 下有意义
    - 当结果审查认为某实验不支持论文主张时，按诊断回写代码并重跑/复审
    - 后端可选 `llm` 或 `codex`
    - 只有错配减少且覆盖不丢失，修复才会被保留

14. **报告输出**
    - `risk_report.json`
    - `review.md`
    - `review.docx`
    - `result_review.json/md/docx`（若生成）
    - `generated_files.json`
    - `run_cost.json`

## 5. 关键模块

| 模块 | 角色 |
|---|---|
| `cli.py` | CLI 参数、doctor/status/review 命令 |
| `config.py` | 环境变量和 Windows 注册表配置读取，构建 LLM 客户端 |
| `llm.py` | OpenAI-compatible 文本/多模态客户端 |
| `pipeline.py` | 主流水线编排 |
| `documents.py` | 论文读取和切块 |
| `schema_models.py` / `schemas.py` | Pydantic 真源和 schema 校验 |
| `facts_normalize.py` / `tasks_normalize.py` | LLM 输出归一化 |
| `facts_coverage.py` | 图表/事实覆盖补强 |
| `experiment_index.py` | 本地实验索引 |
| `task_scripts.py` | per-task layout 的任务脚本 manifest 和 harness 注入 |
| `runner.py` | 受限运行、smoke/full 两相、LLM 修复调度 |
| `security.py` | 依赖白名单、危险导入/调用扫描、脱敏 |
| `result_review.py` | 结果级多模态审查 |
| `verdict.py` | 本地复现结论映射 |
| `risk_report.py` / `review_markdown.py` / `docx_writer.py` | 风险报告和文档输出 |
| `web/app.py` / `web/jobs.py` / `web/stages.py` | 极简本地 Web API 和进度展示 |

## 6. 当前功能状态

### 仍存在

- OpenAI-compatible LLM API
- 多模态输入和结果级审查
- 第二事实抽取模型 `GENG_LLM2_*`
- 专用生成模型 `GENG_GEN_*`
- per-task layout
- science-loop / science repair
- Codex CLI science repair 后端
- FastAPI 极简 Web
- schema 导出
- DOCX 报告输出

### 已不存在或不是当前架构

- 独立代码审查 CLI 开关、相关环境变量和模块
- 旧 Web 设计稿里的浏览器监督调度
- 旧监督器模块
- LangGraph 监督循环
- 多 worker Web 队列和完整 settings 页面

### 残留清理状态

源码和当前文档中已清掉旧代码审查路径与旧调度 schema；历史 case 产物或旧生成出的 Word/PDF 不作为当前架构真源。

## 7. 输出目录结构

典型 case：

```text
case_xxx/
  paper_chunks.json
  engineering_facts.json
  engineering_facts_model2.json        # 仅第二抽取模型启用时
  engineering_facts_ensemble.json      # 仅第二抽取模型启用时
  paper_thesis.json                    # 仅 --science-loop 时
  repro_tasks.json
  experiment_index.json
  repro_project_manifest.json
  generated_files.json
  runtime_result.json
  runtime_result_pre_fallback.json     # 仅 fallback 覆盖前失败结果需保留时
  result_review.json
  result_review_error.json
  risk_report.json
  run_cost.json
  review.md
  review.docx
  result_review.md
  result_review.docx
  audit/
    01_extract_engineering_facts.md
    01c_extract_paper_thesis.md
    02_build_repro_tasks.md
    03a_generate_repro_project_plan.md
    03b_generate_repro_project_file_*.md
    04_review_reproduction_results.md
    04a_review_reproduction_experiment_*.md
    raw_*.txt
    validation_*.json
    llm_error_*.json
  repro_project/
    README.md
    requirements.txt
    config.json
    config_smoke.json
    run_experiment.py
    src/
    tasks/                             # 仅 --per-task-layout 时
    outputs/
    repair_logs/
```

## 8. 使用建议

普通完整运行：

```powershell
python -m geng_agent review paper.pdf --out case_001 --run-repro
```

更适合当前架构的强运行配置：

```powershell
python -m geng_agent review paper.pdf --out case_001 `
  --run-repro `
  --per-task-layout `
  --science-loop `
  --repair-attempts 3 `
  --run-timeout 600
```

诊断兜底原因：

```powershell
python -m geng_agent review paper.pdf --out diagnostic_case `
  --run-repro `
  --no-template-fallback `
  --no-result-review `
  --no-resume
```

Web：

```powershell
python -m pip install -e ".[repro,web]"
geng-agent-web
```

浏览器打开 `http://127.0.0.1:8765`。
