# 本地电脑领取云端论文

网站以 `GENG_EXECUTION_MODE=pull` 启动后只负责账号、论文上传、任务状态和最终 ZIP。本地 worker 主动通过 HTTPS 领取论文，在原电脑调用现有 `ReviewPipeline`，再调用现有 `build_delivery` 打包回传。同一个案例目录根路径只允许一个 worker 进程；论文逐篇执行，论文内部 GPU、Writer、Reporter 的并发策略保持原流程设置。

## 本地配置

使用已装好本项目及 `web` 依赖的 Python 3.11+ 环境。配置 JSON 放在仓库之外，例如 `D:\geng-artifacts\local-worker.json`。云端和本地必须设置同一个强随机 `GENG_WORKER_TOKEN`，长度至少 32 字符。下例不包含真实 token：

```json
{
  "url": "https://papers.example.com",
  "case_root": "D:\\geng_cases",
  "poll_seconds": 10,
  "heartbeat_seconds": 20,
  "request_timeout": 60,
  "retry_seconds": 5,
  "max_pdf_bytes": 83886080,
  "env": {
    "GENG_MODEL_CONFIG": "D:\\geng-artifacts\\model-config.json",
    "GENG_SHARED_SCIENCE_PYTHON": "D:\\geng-artifacts\\shared-python\\Scripts\\python.exe"
  }
}
```

`env` 可省略，worker 会沿用当前 Windows 用户已有的模型和科学运行环境。该映射只接受 `GENG_MODEL_CONFIG`、`GENG_SHARED_SCIENCE_PYTHON`、`GENG_PYTHON` 三个非秘密设置。模型配置文件和共享 Python 路径应替换为本机现有路径。`env.GENG_PYTHON` 供后续流程使用；启动 worker 的解释器由启动脚本读取进程环境中的 `GENG_PYTHON` 决定。

token 可保存在当前用户环境变量 `GENG_WORKER_TOKEN`，也可放在本机 JSON 的 `token` 字段；不要把含 token 的配置提交到仓库。命令行没有 `--token` 参数，日志不记录 token，worker 启动后也会从自身进程环境移除该变量，防止模型和工具子进程继承它。云端 URL 只接受 HTTPS 域名根地址；`http://localhost`、`http://127.0.0.1` 和 `http://[::1]` 仅用于本地测试。

配置优先级：`GENG_WORKER_URL`、`GENG_WORKER_TOKEN`、`GENG_WORKER_ID`、`GENG_CASES_ROOT` 环境变量分别覆盖 JSON 的 `url`、`token`、`worker_id`、`case_root`。没有 `case_root` 时使用项目原有案例根目录。`worker_id` 可省略，首次启动会生成固定 UUID 并保存在案例根目录的 `.cloud-worker` 下；同一云端的后续启动会复用它。已产生身份记录后，配置中的 `worker_id` 必须与其一致。

## 启动

在项目目录运行：

```powershell
.\tools\start_local_web_worker.ps1 -Config D:\geng-artifacts\local-worker.json
```

脚本默认使用原有科学 Python 环境；可先设置 `GENG_PYTHON` 为本机现有解释器。也可以直接启动模块：

```powershell
python -m geng_agent.web.local_worker --config D:\geng-artifacts\local-worker.json
```

人工单任务联调使用脚本的 `-Once` 或模块的 `--once`：最多领取一篇，没有排队任务时直接退出；领取后仍完成科学流程和必要的网络重试。单任务模式会调用真实科学流程；自动化测试必须注入模拟 `pipeline_factory`。

## 断网、休眠与恢复

本地材料保存在 `case_root\cloud_<case_id>`，PDF 先完整写入临时文件、校验 PDF 文件头后原子替换；已存在的完整 PDF 会复用。心跳默认每 20 秒运行，网络失败不会取消本地科学流程。HTTP 请求超时只约束一次网络操作，没有科学任务总时限。临时网络错误和 HTTP 408、425、429、5xx 可重试状态会逐步退避，最长间隔 60 秒；网络恢复后继续下载或回传。

科学流程完成后，worker 会先把 `pipeline_complete` 原子写入本地 `.cloud-worker.json`，然后打包、上传 ZIP。打包失败、上传失败或进程重启时会复用该完成标记，避免再次运行已完成的科学过程。失败或取消的待回传状态也先保存在本地，网络恢复后再上报。云端完成状态存在但本地报告或复现工程缺失时，worker 会明确上报交付失败，保留现场供原电脑恢复。

上传的 ZIP 内只有一个 `复现交付包/` 总目录，顶层放两份中文 Word 报告，`复现任务/` 下按任务分目录，每个任务仅含 `代码/`、`复现结果/` 和逐文件说明 `readme.md`。代码目录保留依赖、配置和运行输入，已有结果另放结果目录；审计、日志、交接 JSON、缓存及临时文件留在本地。此整理只影响最终下载包，不删除案例现场，也不重跑科学实验。

`readme.md` 的内容由 Writer 基于真实代码与结果撰写，解释各文件实现的通信模型、算法、实验和结果含义。宿主透传 `delivery_readme.md`，不把列名、字段名或英文源码注释当作给用户的科研解释；补写导读属于文档修订，不另起科学运行。

这项写作职责通过 [`task_delivery_readme.md`](../geng_agent/prompts/task_delivery_readme.md) 注入实际 Writer 提示词，覆盖初次执行、续跑返修和仅准备代码的任务。提示词同时交代最终打包后的配置路径及依赖文件用途，避免导读只适用于 Writer 工作区。以后调整导读的表达要求可直接修改该提示词文件；宿主不会因此新增内容验收或要求重跑实验。

HTTP 400、413、422 等永久上传拒绝会被记录为交付失败，并释放队列让下一篇论文继续执行；修复云端文件大小或上传配置后，在网站重试即可只恢复交付。HTTP 401、403 表示 worker 认证配置需要修复，worker 会停止领取并保留本地完成标记。ZIP 容量限制属于传输和存储配置，不参与科学结果判断。

电脑休眠期间本地计算暂停且无法发心跳；唤醒后继续运行和网络重试。电脑重启后需要重新运行上面的启动命令，云端会把同一 worker 未完成的任务返回给它。科学流程尚未完成时，通过原流程的 `resume=True` 和已有检查点恢复；已完成时只恢复交付。当前脚本以前台运行方式启动，未配置 Windows 自动开机启动。

网站上明确请求取消后，心跳把取消标记交给原有 `progress.check_cancelled()`，由科学流程在支持的安全边界停止。断线或心跳过期不会自动杀进程。保留同一 `case_root`、`.cloud-worker` 身份文件、案例目录和模型配置；不要为恢复任务生成新的 worker 身份。操作系统文件锁随进程退出自动释放，不需要手工删除锁文件。
