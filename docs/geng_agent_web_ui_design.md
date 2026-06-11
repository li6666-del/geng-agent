# 耿同学agent Web 当前实现说明

> 生成日期：2026-06-10。本文件描述当前 `geng_agent/web/` 源码现状，不再保留旧版完整 Web 产品设计稿。

## 1. 当前边界

当前 Web 是本地极简进度面板，目标是让用户从浏览器提交一篇 PDF，并实时查看 `ReviewPipeline` 的阶段进度。它不是完整的多用户平台，也不是旧设计中的监督调度系统。

当前不存在：

- 浏览器侧旧监督调度模式
- 监督时间线
- settings 页面
- 多 worker 队列
- 完整 React/TypeScript SPA
- Web 端完整 CLI 参数映射
- Web IDE 嵌入

## 2. 源码入口

| 文件 | 作用 |
|---|---|
| `geng_agent/web/__main__.py` | `uvicorn` 启动入口，默认 `127.0.0.1:8765` |
| `geng_agent/web/app.py` | FastAPI 路由 |
| `geng_agent/web/jobs.py` | PDF 保存、URL 下载、单任务队列、后台线程运行 pipeline |
| `geng_agent/web/stages.py` | 根据 case 产物推导阶段进度 |
| `geng_agent/web/static/index.html` | 单页静态前端 |

启动：

```powershell
python -m pip install -e ".[repro,web]"
geng-agent-web
```

也可直接：

```powershell
python -m geng_agent.web --host 127.0.0.1 --port 8765
```

## 3. API

| 方法 | 路径 | 返回/行为 |
|---|---|---|
| `GET` | `/` | 返回 `web/static/index.html` |
| `GET` | `/api/health` | LLM 配置是否可构建、cases root、当前 active run |
| `POST` | `/api/runs` | 接收 `pdf_file` 或 `pdf_url`，创建 run 并启动后台线程 |
| `GET` | `/api/runs/{run_id}` | 查询 run 状态、case 目录、下一阶段、阶段列表 |
| `GET` | `/api/runs/{run_id}/stream` | SSE 推送进度变化和 finished 事件 |

`POST /api/runs` 约束：

- `pdf_file` 和 `pdf_url` 二选一
- 只接受 PDF
- 单文件最大 80 MB
- URL 仅允许 `http` / `https`
- 同时只能有一个 queued/running 任务

## 4. 后台任务模型

`jobs.py` 中的 `_runs` 是进程内字典，`_lock` 保护状态读写。任务提交后：

1. 保存上传 PDF 或下载 URL PDF
2. 写入 `.geng/meta.json`
3. 创建 `RunRecord`
4. 启动 daemon thread
5. 在线程中构建 `build_llm_client()`
6. 调用：

```python
ReviewPipeline(client=client).run(
    paper_path=record.paper_path,
    output_dir=record.case_dir,
    run_repro=True,
    result_review=True,
    resume=False,
    template_fallback=True,
)
```

因此 Web 当前固定行为是：

- 总是运行复现
- 总是尝试结果级审查
- 不复用旧阶段产物
- 允许 template fallback
- 使用主模型配置
- 不暴露 `GENG_LLM2_*` / science-loop 等高级能力；第三轮固定走 Codex writer/reviewer 工作流

## 5. 进度来源

Web 不维护独立阶段状态机，而是调用 `inspect_case_status(case_dir)` 读取当前 case 产物，再由 `build_stage_progress(...)` 转成前端可显示的阶段列表。

关键产物：

- `paper_chunks.json`
- `engineering_facts.json`
- `repro_tasks.json`
- `experiment_index.json`
- `repro_project_manifest.json`
- `runtime_result.json`
- `result_review.json` / `result_review_error.json`
- `risk_report.json`
- `review.md`

SSE 只在状态签名变化时发送事件，避免无意义刷屏。任务完成或失败后发送 `finished` 事件。

## 6. 配置

Web 复用 CLI 的主模型配置：

```powershell
$env:GENG_LLM_API_KEY="你的 API Key"
$env:GENG_LLM_BASE_URL="https://api.openai.com/v1"
$env:GENG_LLM_MODEL="你的多模态模型"
```

case 根目录：

```powershell
$env:GENG_CASES_ROOT="C:\Users\84475\Documents\geng_cases"
```

未设置时默认 `~/Documents/geng_cases`。

## 7. 当前限制

- 进程重启后 `_runs` 内存状态丢失，但 case 目录仍在。
- 单任务模型不适合批量并发跑论文。
- Web 未暴露 CLI 高级参数，复杂运行仍建议用 CLI。
- Web 健康检查只验证主 LLM client 能否构建，不做真实 API 试调用。
- PDF URL 下载没有代理/重试/下载进度。
- 没有取消任务接口。
- 没有鉴权，默认只应绑定本机地址。

## 8. 后续可做但尚未实现

- 从 `.geng/meta.json` 和 case 目录恢复历史 run 列表
- 暴露有限高级参数：`science_loop`、`codex_agent_rounds`、`codex_agent_timeout`
- 增加取消任务接口
- 增加多任务批处理时的独立 worker 设计
- 增加结果图、CSV、报告文档的浏览和下载入口
