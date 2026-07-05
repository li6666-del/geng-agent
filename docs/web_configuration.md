# Web 配置参考

新版五阶段 Web 使用环境变量配置。生产环境建议将变量放在部署平台的 Secrets Manager 中；本地 Docker Compose 可复制 `.env.example` 为 `.env`。

## 必填配置

| 变量 | 示例 | 说明 |
|---|---|---|
| `GENG_LLM_API_KEY` | `sk-...` | OpenAI-compatible 模型密钥。不要提交到 Git。 |
| `GENG_LLM_MODEL` | `gpt-5` | Web worker 使用的主模型名称。 |
| `POSTGRES_PASSWORD` | `change-me` | Compose 中 PostgreSQL 用户 `geng` 的密码。生产环境必须替换默认值。 |

## 服务与存储

| 变量 | 默认值 | 说明 |
|---|---|---|
| `GENG_DATABASE_URL` | 本地模式为 `sqlite:///<cases_root>/geng_web.db` | 生产 Compose 使用 `postgresql+psycopg://geng:<password>@postgres:5432/geng`。 |
| `GENG_REDIS_URL` | `redis://127.0.0.1:6379/0` | Celery broker/backend 与 SSE 实时通知。 |
| `GENG_CASES_ROOT` | Windows 为 `~/Documents/geng_cases` | PDF、代码、运行输出、报告和 ZIP 的根目录。API 与 worker 必须挂载同一目录。 |
| `GENG_MAX_PDF_BYTES` | `83886080` | PDF 上传及 URL 导入的最大字节数，默认 80 MB。 |

## 模型端点

| 变量 | 默认值 | 说明 |
|---|---|---|
| `GENG_LLM_BASE_URL` | `https://api.openai.com/v1` | 主模型的 OpenAI-compatible API 根地址。 |
| `GENG_CODE_REVIEW_MODEL` | 未启用独立模型 | 可选的异构代码审查模型。 |
| `GENG_CODE_REVIEW_API_KEY` | 回落到 `GENG_LLM_API_KEY` | 独立审查模型密钥。 |
| `GENG_CODE_REVIEW_BASE_URL` | 回落到主模型端点 | 独立审查模型 API 地址。 |
| `GENG_LLM2_MODEL` | 未启用 | 可选的第二个多模态事实抽取模型。 |
| `GENG_LLM2_API_KEY` | 未启用 | 第二抽取模型密钥。 |
| `GENG_LLM2_BASE_URL` | 未启用 | 第二抽取模型 API 地址；三个 `GENG_LLM2_*` 必须一起设置。 |

## Web 行为开关

| 变量 | 默认值 | 说明 |
|---|---|---|
| `GENG_ENABLE_URL_IMPORT` | `false` | 是否允许服务端下载 HTTPS PDF。默认关闭以缩小 SSRF 攻击面。 |
| `GENG_CELERY_EAGER` | `false` | 测试专用。开启后使用后台线程执行任务，不依赖 Redis worker；生产环境不要开启。 |

## Compose 示例

`.env`：

```dotenv
POSTGRES_PASSWORD=replace-with-a-long-random-password
GENG_LLM_API_KEY=replace-me
GENG_LLM_BASE_URL=https://api.openai.com/v1
GENG_LLM_MODEL=gpt-5
GENG_ENABLE_URL_IMPORT=false
```

启动与迁移：

```bash
docker compose up -d --build
docker compose exec api alembic upgrade head
docker compose ps
```

检查：

```bash
curl http://127.0.0.1:8765/api/v1/health
curl http://127.0.0.1:8765/api/v1/metrics
```

## 资源与并发

- `docker-compose.yml` 中 worker 默认 `--concurrency=2`，每个 worker 的 `prefetch=1`。
- 计算密集型论文优先增加机器资源，再增加 worker concurrency；不要按在线用户数直接放大计算并发。
- 多台 worker 机器部署前，应将 `LocalArtifactStore` 替换为共享 S3/MinIO 实现，或确保所有节点使用同一可靠共享卷。
- API 可以横向扩容；PostgreSQL 是任务与事件状态真源，Redis 只承担队列和低延迟通知。

## 安全注意事项

- `.env`、API key、数据库密码不得提交到仓库。
- 正式团队环境必须通过 HTTPS 暴露服务，并限制 PostgreSQL、Redis 只在内部网络访问。
- 公网部署前应在预留授权层接入 OIDC；当前可信内网模式不提供用户登录。
- 建议每天备份 PostgreSQL，并对 case 共享卷执行增量备份。
