# ADR-0002：PostgreSQL、Redis 与 Celery

## 状态

Accepted

## 决策

PostgreSQL 保存案例、任务、事件和产物索引，是状态真源；Redis 仅承担 Celery broker/backend 和低延迟通知；Celery worker 使用 `acks_late`、`prefetch=1` 与受控并发。

## 后果

- API 重启、Redis 短暂故障不会丢失业务状态。
- worker 丢失时任务可重新投递，并通过 Pipeline 缓存恢复。
- 本地开发需要 Redis；测试可显式启用 eager 模式。

## 备选方案

- SQLite：并发写入和横向 worker 扩展余量不足。
- Redis 作为唯一状态源：不满足审计与恢复要求。
