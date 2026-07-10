# 耿同学agent Web 当前实现说明

> 更新日期：2026-07-10。本文件只描述当前 `geng_agent/web/` 实现。

## 1. 当前边界

Web 是本地极简进度面板：提交一篇 PDF，后台运行 Codex 主流程，实时展示阶段和最终报告。它不是多用户平台，也不包含浏览器 IDE、复杂 settings、监督时间线或多 worker 队列。

## 2. 源码入口

| 文件 | 作用 |
|---|---|
| `web/__main__.py` | uvicorn 启动入口，默认 `127.0.0.1:8765` |
| `web/app.py` | FastAPI 路由与 Codex CLI 健康检查 |
| `web/jobs.py` | PDF 保存/下载、单任务队列、后台 pipeline |
| `web/stages.py` | 根据 case 产物生成阶段进度 |
| `web/static/index.html` | 单页静态前端 |

## 3. API

| 方法 | 路径 | 行为 |
|---|---|---|
| `GET` | `/` | 返回静态页面 |
| `GET` | `/api/health` | 返回 Codex 命令/路径、cases root、active run |
| `POST` | `/api/runs` | 接收上传 PDF 或 PDF URL 并启动任务 |
| `GET` | `/api/runs/{run_id}` | 返回运行状态和阶段进度 |
| `GET` | `/api/runs/{run_id}/stream` | SSE 推送进度和结束事件 |

上传文件与 URL 二选一，只接受 PDF，单文件最大 80 MB，同时只允许一个 queued/running 任务。

## 4. 后台固定行为

`jobs.py` 直接构建 `ReviewPipeline()`，固定使用：

```python
pipeline.run(
    paper_path=record.paper_path,
    output_dir=record.case_dir,
    run_repro=True,
    result_review=True,
    resume=False,
    analysis_fallback=True,
    analysis_backend="codex",
    codex_agent_rounds=5,
)
```

因此 Web 默认使用 Codex 完成前两阶段和任务级 writer，全量运行每个任务并生成 writer 自审报告。Web 不暴露旧 LLM backend 或第三轮模式开关。

## 5. 进度与配置

Web 不维护另一套状态机，而是读取 case 产物并调用 `inspect_case_status()`。主要阶段为论文解析、工程事实、复现任务、实验索引、任务 writer、运行覆盖、结果报告和主报告。

Codex 配置：

```powershell
$env:GENG_CODEX_CMD="codex"
$env:GENG_CODEX_ANALYSIS_CMD="codex"
$env:GENG_CODEX_TASK_WRITER_CMD="codex"
```

case 根目录通过 `GENG_CASES_ROOT` 设置，未设置时默认 `~/Documents/geng_cases`。

## 6. 当前限制

- 进程重启后内存中的 run 状态丢失，但 case 目录仍保留。
- 单 active run，不适合 Web 批量并发论文。
- PDF URL 下载没有代理配置、断点续传或下载进度。
- 没有取消接口和鉴权，默认只绑定本机地址。
- 高级超时、并发和 gap-round 参数仍需 CLI。
