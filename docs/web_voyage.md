# 五阶段“研究航行日志”Web

新版 Web 将内部九个文件检查点聚合为五个用户阶段：论文解构、复现设计、工程构建、执行与自修正、证据审查。CLI 与 case 目录格式保持不变。

## 本地开发

```powershell
python -m pip install -e ".[web,repro]"
cd geng_agent/web/frontend
npm install
npm run build
cd ../../..
geng-agent-web --host 127.0.0.1 --port 8765
```

本地仍需 PostgreSQL 与 Redis；自动化测试可设置 `GENG_CELERY_EAGER=true` 使用进程内 worker。URL 导入默认关闭，只有显式设置 `GENG_ENABLE_URL_IMPORT=true` 后才开放。

## Linux Docker Compose

复制 `.env.example` 为 `.env`，填写模型配置和数据库密码后启动：

```bash
docker compose up -d --build
docker compose ps
curl http://127.0.0.1:8765/api/v1/health
```

服务包括：

- `api`：两个 FastAPI/Uvicorn 进程，提供网页、REST 与 SSE。
- `worker`：默认并发 2、prefetch 1 的 Celery worker。
- `postgres`：案例、任务、事件和产物索引的持久化真源。
- `redis`：Celery broker/backend 与 SSE 实时唤醒通知。
- `cases-data`：API 与 worker 共享的 case 目录卷。

数据库迁移：

```bash
docker compose exec api alembic upgrade head
```

## 核心 API

| 方法 | 路径 | 用途 |
|---|---|---|
| `POST` | `/api/v1/cases` | 流式上传 PDF 并创建任务 |
| `GET` | `/api/v1/cases` | 查询案例列表 |
| `GET` | `/api/v1/cases/{id}` | 查询五阶段状态和产物 |
| `POST` | `/api/v1/cases/{id}/jobs` | 从缓存恢复或重新运行 |
| `POST` | `/api/v1/jobs/{id}/cancel` | 请求在安全边界取消 |
| `GET` | `/api/v1/jobs/{id}/events/live` | 支持 `Last-Event-ID` 的 SSE |
| `GET` | `/api/v1/artifacts/{id}` | JSON、CSV、代码和 Markdown 预览 |
| `GET` | `/api/v1/artifacts/{id}/content` | 查看或下载单个产物 |
| `POST` | `/api/v1/cases/{id}/exports` | 生成阶段或整案 ZIP |
| `GET` | `/api/v1/metrics` | Prometheus 文本指标 |

## 安全边界

- 上传按块读取，限制 80 MB，并校验 `%PDF-` 魔数。
- URL 导入仅允许 HTTPS，逐次重定向校验公网 IP，并将 DNS 结果固定到实际连接。
- 产物服务拒绝绝对路径、`..`、越过 case 根目录及符号链接逃逸。
- React 只按文本渲染 JSON、Markdown、代码和模型输出，不执行产物内 HTML。
- 首期按可信内网部署；统一案例授权函数已经预留，正式公网部署前必须接入 OIDC。

## 恢复与运维

- worker 使用 late acknowledgement、worker-lost 重投递、有限指数退避和 Pipeline `resume=True`。
- PostgreSQL 保存完整事件序列；Redis 故障时 SSE 自动回落到数据库轮询。
- `/api/v1/metrics` 暴露队列深度、各状态任务数量和平均任务耗时。
- 历史案例可通过 `POST /api/v1/cases/import` 登记，不移动原目录。
