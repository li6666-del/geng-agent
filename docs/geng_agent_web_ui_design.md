# 耿同学agent Web 当前实现说明

> 更新日期：2026-09-14。本文件只描述当前 `geng_agent/web/` 实现。

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

前端页面按当前项目分工组织：

| 文件 | 页面职责 |
|---|---|
| `frontend/src/App.tsx` | 案例库、搜索筛选、PDF 点击上传与拖放、创建案例 |
| `frontend/src/CaseWorkspace.tsx` | 流程概览、逐任务核验、两份中文报告入口、历史与实时事件、停止／恢复 |
| `frontend/src/Artifacts.tsx` | 全部产物搜索、阶段／类型筛选、分页、预览与下载 |
| `frontend/src/ReportPreview.tsx` | Editor Markdown 阅读视图；保留原文、表格和成对图片，不执行 HTML |
| `frontend/src/presentation.ts` | 中文状态名称、当前流程说明、事件去重、案例内图片路径映射 |
| `frontend/src/ui.tsx` | 公共控件、原生 dialog 焦点管理 |
| `frontend/src/styles.css` | 单一全局样式及桌面／移动端适配 |
| `web/case_view.py` | 只读投影已保存的任务计划、Reporter 核验与报告编辑状态，不重判科学结论 |

案例详情接口附带最近 40 条已保存事件；前端合并 SSE 事件并按 ID 去重。重新打开已结束案例仍能查看历史记录。界面将“流程已结束”、Reporter 的科研结论、工程异常和报告文件可用性分别展示。缺失核验记录保持未知，不因代码执行通过或报告存在显示“已复现”。

视觉采用浅色简约风格：浅灰页面、白色内容区、细分隔线，少量蓝色用于操作与当前阶段提示。首页去掉波形、发光效果和英文装饰标签，案例总数与处理中数量来自实际案例接口。工作区侧栏随当前阅读位置高亮，报告中的原文与复现图保留原色，可点击打开原图。

结果对比报告和本地复现报告提供独立入口，支持在线阅读、Markdown 和 Word 下载。`review.md` 仍作为导航文件在全部产物中提供。报告图片只解析当前案例目录中已登记的相对路径；未知或外部图片标为不可用，不从外部加载。公式源码保留；预览暂不做 LaTeX 排版，复杂排版可下载原文件。产物分页可访问全部匹配文件，下载全部／本阶段不受文件名和类型筛选影响。

## 3. 五阶段映射

Web 进度来自 `geng_agent.progress`，阶段依次为：

1. 论文理解：版面解析，联合提取事实、主张与缺口。
2. 实验规划：按需回补，联合产出任务、验收依据与科学架构。
3. 复现与独立核验：按需构建共享 Foundation，Writer 执行与修订，Reporter 独立核验。
4. 中文报告编辑：已有 Report Editor 撰写两份详细中文报告与一份简短导航。
5. 项目与报告交付：Markdown 转 Word，整理项目及执行证据。没有独立环境重建验证。

上述为当前页面名称；保留原阶段 ID 和内部步骤 ID，以兼容历史案例和现有流程事件，不把内部步骤数量当成 Agent 调用次数。

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
- 超长文本预览会明确标为节选，完整报告和数据可下载原文件查看。

## 7. 界面验证

- 2026-09-13：Web 专项 21 项通过。
- 2026-09-14 浅色版：前端 Vitest 7 项通过；TypeScript 与 Vite 构建通过，`dist/` 已更新。
- 使用临时数据库和禁用论文作业分发的独立服务，通过 Edge 无头浏览器检查浅色版桌面与 390px 手机布局，无页面 JavaScript 错误或整页横向溢出。另检查了 320、700、900、1280px 下的首页和案例页，未发现整页横向溢出。
- 实际检查了报告图文预览、历史事件恢复、PDF 拖放与创建、停止请求、恢复中断案例、产物分页与筛选、ZIP 下载、404 提示。
- 验收样例包含复制的旧论文核验记录和图片；未修改原案例，未执行新论文实验或模型调用。界面测试不构成科研复现质量验证。
