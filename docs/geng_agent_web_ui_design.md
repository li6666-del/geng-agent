# 耿同学agent Web 当前实现说明

> 更新日期：2026-07-15。本文件只描述当前 `geng_agent/web/` 实现。

## 1. 当前边界

Web 是主流程的本地操作台：上传论文、持久化任务状态、展示五阶段进度、预览产物并导出 ZIP。它直接调用 `ReviewPipeline.run()`，不复制论文分析、writer 或 reporter 逻辑。当前没有用户认证，默认仅绑定 `127.0.0.1`，不应直接暴露到公网。

## 2. 源码入口

| 文件 | 作用 |
|---|---|
| `web/__main__.py` | uvicorn 启动入口，默认 `127.0.0.1:8765` |
| `web/app.py` | 稳定的 FastAPI 公共入口 |
| `web/app_v2.py` | 案例、任务、产物、导出和基础 SSE API |
| `web/app_runtime.py` | Redis 辅助的实时事件与 Prometheus 指标 |
| `web/tasks.py` | Celery/eager worker 与主流程适配器 |
| `web/artifacts.py` | 案例内路径校验、产物目录和 ZIP 导出 |
| `web/frontend/` | React 源码及随 Python 包发布的 `dist/` |

## 3. 五阶段映射

Web 进度来自 `geng_agent.progress`，阶段依次为：

1. 论文解构：版面解析与初始事实抽取。
2. 复现设计：初步任务、定向事实回补、任务定稿与实验索引。
3. 任务级复现：writer 生成、full 运行和逐任务核验。
4. 报告编排：独立 reporter 汇总并编辑三份报告。
5. 交付物生成：Markdown 转 Word 及最终元数据落盘。

事件写入数据库，案例页通过 SSE 接收；历史案例没有事件时，系统会根据磁盘 checkpoint 恢复阶段状态。

## 4. 运行与存储

本地默认使用 SQLite 和进程内 Celery eager worker：

```powershell
pip install -e ".[web,repro]"
geng-agent-web
```

关键环境变量：

| 变量 | 作用 |
|---|---|
| `GENG_CASES_ROOT` | case 根目录 |
| `GENG_DATABASE_URL` | SQLAlchemy 数据库地址；未设置时使用 case 根目录下 SQLite |
| `GENG_REDIS_URL` | Redis 与 Celery broker/backend 地址 |
| `GENG_CELERY_EAGER` | `1` 时在当前进程后台线程执行 |
| `GENG_ENABLE_URL_IMPORT` | `1` 时开放带 DNS 固定和公网 IP 校验的 HTTPS PDF 下载 API |

生产式队列可使用 PostgreSQL、Redis 和独立 Celery worker。取消是安全边界协作停止，不会在外部 Codex 调用或科学计算中间强杀进程。

## 5. 产物策略

报告、复现代码、结果图、CSV 和顶层证据会进入 Web 产物目录。体积较大的 `audit/` 及已生成的 `exports/` 不进入默认索引，但原文件仍保留在 case 目录。所有读取和 ZIP 导出都经过案例根目录约束，拒绝绝对路径、`..` 和越界符号链接。

## 6. 当前限制

- 目前没有账号、租户隔离和远程访问鉴权。
- eager 模式适合单机使用；多任务部署应启用外部 Celery worker。
- URL 导入默认关闭，且只支持 HTTPS。
- 取消只在主流程安全边界生效，已经开始的单次外部调用不会立即终止。
