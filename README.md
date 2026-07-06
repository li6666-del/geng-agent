# 耿同学 Agent

面向通信论文的自动复现与可信审查系统。它不判断论文真伪，而是把论文拆成可追溯事实、可运行任务、任务级复现实验、图像证据对比与人工可读报告，帮助研究者判断复现结果是否支持论文结论。

当前主线默认使用 Codex CLI 子进程完成论文理解和任务级复现；OpenAI-compatible LLM 路径继续作为显式兼容选项。CLI 与 Web 共用同一套 `ReviewPipeline`、安全运行器和 case 目录。

## 五阶段工作流

| 用户阶段 | 内部节点 | 代表产物 |
|---|---|---|
| 论文解构 | `paper`、`engineering_facts` | 论文文本块、页面证据、工程事实 |
| 复现设计 | `repro_tasks`、`experiment_index` | 复现任务、目标图表与指标 |
| 工程构建 | `repro_project_manifest`、`repro_project` | 代码、配置、依赖、工程清单 |
| 执行与自修正 | `runtime`、repair logs | 运行轮次、修复记录、PNG/CSV |
| 证据审查 | `result_review`、`review` | 图像对比、风险结论、Word/Markdown 报告 |

第三阶段采用任务级自治 writer：每个任务在独立 sandbox 中生成代码；开启运行后，通过 guard 执行 full、对照论文证据并自我修正。主持人负责结构验收、安全检查、产物合并和最终报告。

## 安装

要求 Python 3.11+。

```bash
git clone https://github.com/li6666-del/geng-agent.git
cd geng-agent

# CLI 与常用复现依赖
python -m pip install -e ".[repro]"

# Web、任务队列与数据库支持
python -m pip install -e ".[repro,web]"
```

安装后运行环境自检：

```bash
python -m geng_agent doctor
```

## Codex 与兼容模型配置

默认流程使用 Codex CLI：

```bash
set GENG_CODEX_CMD=codex
set GENG_CODEX_ANALYSIS_CMD=codex
set GENG_CODEX_TASK_WRITER_CMD=codex
```

旧 LLM analysis 兼容路径需要设置：

```bash
set GENG_LLM_API_KEY=...
set GENG_LLM_BASE_URL=https://api.openai.com/v1
set GENG_LLM_MODEL=...
python -m geng_agent review paper.pdf --out case_001 --analysis-backend llm
```

## CLI

只生成审查包和复现工程：

```bash
python -m geng_agent review paper.pdf --out case_001
```

运行完整复现：

```bash
python -m geng_agent review paper.pdf --out case_001 --run-repro
```

检查断点状态：

```bash
python -m geng_agent status case_001
```

常用选项包括 `--analysis-backend`、`--facts-gap-rounds`、`--tasks-gap-rounds`、`--science-loop`、`--codex-agent-rounds`、`--run-timeout`、`--no-result-review` 与 `--no-resume`。使用 `python -m geng_agent review --help` 查看完整参数。

## Web：研究航行日志

Web 已升级为 React + FastAPI 模块化单体：

- PostgreSQL 保存案例、任务、事件、产物索引和导出状态。
- Redis 承担 Celery broker/backend 与 SSE 低延迟通知。
- Celery worker 默认并发 2、prefetch 1；同一案例只允许一个活跃任务。
- SSE 支持 `Last-Event-ID` 断线续传；Redis 不可用时回落到数据库轮询。
- 支持单文件下载、阶段 ZIP 和整案 ZIP。
- 五章滚动页展示实时阶段、内部步骤、运行错误与阶段产物。

### Linux Docker Compose（推荐）

```bash
cp .env.example .env
# 编辑 .env，填写模型配置与数据库密码
docker compose up -d --build
docker compose ps
```

浏览器打开 `http://127.0.0.1:8765`。

健康检查与指标：

```bash
curl http://127.0.0.1:8765/api/v1/health
curl http://127.0.0.1:8765/api/v1/metrics
```

数据库迁移：

```bash
docker compose exec api alembic upgrade head
```

### 本地前端开发

```bash
cd geng_agent/web/frontend
npm install
npm run dev
```

生产静态文件使用 `npm run build` 构建。详细 API、恢复策略和安全边界见 [`docs/web_voyage.md`](docs/web_voyage.md)，关键架构决策见 [`docs/adr/`](docs/adr/)。

## 输出目录

```text
case_001/
  paper/
  paper_chunks.json
  engineering_facts.json
  repro_tasks.json
  experiment_index.json
  repro_project_manifest.json
  runtime_result.json
  risk_report.json
  review.md
  review.docx
  result_review.json
  result_review.md
  result_review.docx
  audit/
  exports/
  repro_project/
    README.md
    requirements.txt
    tasks_manifest.json
    configs/
    tasks/
    outputs/
    repair_logs/
```

- `review.md/docx`：事实、任务、运行、风险与结果审查总报告。
- `result_review.md/docx`：逐任务的论文证据与本地结果对比。
- `runtime_result.json`：安全扫描、运行阶段、返修和产物统计。
- `risk_report.json`：缺失信息、fallback、运行异常和可信度汇总。
- `audit/`：Prompt、模型原始输出、校验、运行和修复证据链。

## 安全边界

- 生成代码只能通过既有安全运行器执行，依赖与 import 受白名单约束。
- Web 上传采用分块写入、大小限制、PDF 魔数校验与原子落盘。
- URL 导入默认关闭；启用后仅接受 HTTPS，并阻止内网地址、DNS 重绑定和无限重定向。
- 产物接口拒绝绝对路径、`..`、越过 case 根目录与符号链接逃逸。
- 论文文本、日志、Markdown、JSON 与生成代码均视为不可信数据，只读展示，不作为网页指令执行。
- 首期身份模型为可信团队内网；公网部署前应在预留授权层接入 OIDC。

## 项目定位

本项目评估的是工程复现风险与证据充分性，不是学术不端检测器。模板 fallback 只是安全网；若最终产物与论文目标不一致，应明确报告为复现失败或证据不足，而不能把“成功运行模板”包装成“成功复现论文”。
## Reproduction benchmark

The offline benchmark scorer compares pipeline artifacts with hidden, expert-curated facts, tasks, implementation checks, and numerical reference curves. It reports a gated total score plus seven diagnostic dimensions.

```powershell
python -m geng_agent benchmark benchmarks/communication_v1/suite.json --validate-only
python -m geng_agent benchmark benchmarks/communication_v1/suite.json --runs runs --out benchmark_results
```

The bundled three-paper suite is intentionally marked `gold_status=pending` until author code/results and expert annotations are curated, so unverified labels cannot influence the score. See `docs/benchmark.md` for the case contract, run layout, scoring rules, and 18-paper curation target.
