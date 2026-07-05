# ADR-0001：采用模块化单体与独立 worker

## 状态

Accepted

## 决策

保留 FastAPI 与 `ReviewPipeline`，将 HTTP API、领域模型和产物服务组织为模块化单体；耗时复现由独立 Celery worker 执行。暂不拆分微服务。

## 后果

- API 可独立扩容，任务故障不会阻塞网页请求。
- 仍保持一个 Python 代码库和一次版本发布。
- API、worker 与 Pipeline 之间必须维持稳定的事件和数据库契约。

## 备选方案

- 继续后台线程：无法跨进程恢复，拒绝。
- 完整微服务：对 10–30 人团队运维成本过高，暂缓。
