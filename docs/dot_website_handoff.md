# Your dot 网站与项目云端管理交接单

更新日期：2026-10-07（北京时间）。

## 交接目标与当前状态

用户已明确目标：由 Your dot 同时管理网站和项目，由 Dot 调度 Codex Cloud 在云端实际运行本项目、执行论文复现并回传成果，最终不依赖这台 Windows 电脑持续开机。管理对象是 `https://bupt-gengagent.cn`、项目代码与配置、云端执行环境及论文任务。

这份交接单目前是迁移目标与现状资料，尚未完成云端环境配置或真实云端全流程验证。此前“Dot 连接本机并管理本地 worker”的方案仅是迁移前的运维方式，不是用户要求的最终架构。

后续已进一步明确：优先核实能否直接在 Dot 自带的云电脑上运行已安装的 Codex CLI；单独创建 Codex Cloud 环境是另一条可选路线。当前仅完成部分能力检查，云端 CLI 初始化遇到只读文件系统问题，迁移保持暂停。下文的 Codex Cloud 设计仍是待验证方案，不代表已决定必须使用独立 Cloud 环境。

## 本地项目保护要求（2026-10-07）

用户要求先提交、推送当前本地项目，作为迁移前恢复基点。保护标签使用 `pre-cloud-migration-20261007`；提交和外部 Git bundle 的实际结果记录在 `D:\geng-artifacts\reports\precloud1007-save.json`，以记录和远端核验为准。

正式恢复迁移后，使用单独的工作副本、工作树或云端工作区，从该基点开展适配。原 `D:\耿同学agent`、本地 worker 配置、共享 Python、worker 身份和实验现场继续保留。云端完整执行、成果回传和恢复能力验证完成并获得切换授权后，才调整原有生产运行。不要把云端修改直接覆盖回本地，也不通过 reset、强制推送或清理案例来简化迁移。

2026-10-07 在本机已核对：网站 `/api/v1/health` 返回 HTTP 200；`GengAgent-LocalWorker` 计划任务为 Running，存在对应 Python worker 进程；每周清理任务已存在。服务器内部路径下文来自部署配置和已有部署记录，本次未通过 SSH 重新核实。接手时以当前服务器实际配置为准。

## 给 Dot 的职责

- 维护网站可用性，检查登录、论文提交、排队、结果下载和成果详情页；诊断相关日志与异常，处理网站范围内的修复、部署和回滚。
- 管理项目版本、依赖、运行配置和云端 Codex 环境；调度云端任务，观察运行、处理异常并确认最终交付。科学执行继续沿用现有项目，Dot 负责全局协调。
- 迁移完成前维持现有本地接单服务；迁移后管理云端执行任务及其持久化现场。恢复同一任务时保留对应 worker 身份、案例目录、云端任务关联和断点，先确认实际运行状态；不要因一次心跳失败或暂时没有输出而停止实验。
- 跟进用户已提交任务的交付问题。科学流程完成而上传失败时，优先恢复打包和上传，使用已有结果。
- 按已有每周清理机制维护存储：已结束的网站任务至少保留 7 天，再按周清理交付 ZIP 和对应本地实验现场；原论文、账号和任务历史保留。沿用现有清理任务，不另建一套重复清理。
- 向用户报告确切故障、需要用户处理的连接问题和重要处理结果。正常运行时保持安静；说明观察到的事实、采取的动作及恢复结果。

同一服务器还承载 `myhanbaobao.cn`。操作应针对耿同学网站的服务、配置和数据；共享 Nginx 的调整需先验证配置并保持另一个网站正常运行。

科学结论由各任务的独立 Reporter 给出，修复路径沿用现有主持人机制。网站的 succeeded 表示工程交付成功，不表示所有论文结论都已复现。网站管理不能把“部分复现”“带假设复现”改写成完全复现，也不应为了处理展示或传输问题另起一遍付费科学运行。

## 目标架构：Dot 管理，Codex Cloud 执行

```text
用户上传论文到现有网站
        ↓
Dot 获取待办并调度云端 Codex
        ↓
云端 Codex 环境运行现有项目
        ↓
论文理解 → 规划 → Writer / Reporter → 中文 Word → ZIP
        ↓
现有网站接收结果并提供下载
```

Dot 负责网站、项目维护、调度与异常处理；Codex Cloud 提供运行代码和命令的执行环境；现有网站继续保存账号、论文、任务状态和最终 ZIP。迁移运行位置不自动更换项目内部的 LLM，初步按当前 DeepSeek Flash/max 配置准备，另有模型要求再明确调整。

官方文档已确认 Dot 可以在用户预先配置好的 Codex Cloud 环境中新建编码任务，用户电脑可以离线。以下是本项目的实施建议，尚不是已验证的云端功能：

1. **准备准确源码**：当前本地有尚未提交的修复和架构改动，不能认为 GitHub main 已包含它们。先确定要迁移的源码版本，再把项目和云端适用的执行说明提供给 Cloud 环境；不复制本机凭据、虚拟环境或缓存。项目 AGENTS.md 当前按本机执行编写，实施云端迁移时需同步明确云端适用的目录和执行范围。
2. **准备运行环境**：配置 Python、项目依赖及符合本项目所需命令能力的 Codex CLI。项目自身还会启动 Codex 子进程和科学沙箱；外层 Cloud 任务能用工具，并不证明内层 CLI、模型认证和沙箱可以直接运行，必须在所选环境验证。
3. **适配联网与凭据**：云端需访问网站、模型服务和所需资料/依赖。当前 `codex_provider.py` 的 direct 模式与 `security_env.py` 的科学环境会绕过代理；迁移需遵从云端实际网络与凭据注入方式，不能直接照搬本机直连策略。网站 worker token 和模型凭据通过云端受支持配置提供。
4. **保持运行现场与身份对应**：现有 `local_worker.py --once` 已支持一次领取并完成一篇论文，可作为初期云端执行入口。初版宜由 Dot 继续同一个专用云端 Codex 任务，使用固定云端 worker 身份和案例根目录，按篇调用该入口；恢复也回到同一任务。不要让多个独立 VM 共享同一 worker_id 并同时领取，否则现有 claim 协议可能把同一运行任务返回给它们。若以后改成每篇论文一个独立云任务，应增加明确的论文、worker、云任务、持久化现场关联，不能靠临时工作区自动恢复。
5. **验证新任务触发**：网站提交论文不会自动成为 Dot 的事件。需要为 Dot 配置确实可用的待办查询与唤醒方式，并确认它能启动或继续对应 Cloud 任务。不能仅凭一句“已接管”认定无人值守链路已经建立；不默认让一个无人观察的后台进程无限常驻。
6. **验证运行和交付**：先检查依赖、网络、CLI 与进程隔离，再用一篇小计算量论文验证云端运行、两份中文 Word、按任务代码/结果/README、ZIP 上传和同任务续跑。Windows AppContainer 仅在 Windows Writer 分支启用，不能推断 Linux/云端已具备相同进程隔离，需要在云端验证或配置等效隔离。
7. **持久化与切换**：最终结果继续回传网站；尚未完成的现场也要有明确保存与恢复位置。官方云任务状态有保留期限，不能把其工作区当永久存储。先证明云端能完成闭环，再切换新任务领取方；旧本地任务的现场和归属需显式处理，不能直接改 worker_id 或关掉正在运行的本地任务。

Codex Cloud 默认 VM 的 CPU、内存和磁盘配置受套餐影响，GPU 能力未在所查默认规格中得到确认。先以 CPU 轻量通信论文验证；依赖 GPU 的论文需核对实际可用设备或另行接入合适的云端算力，不能承诺默认环境可覆盖所有论文。

官方依据：

- https://learn.chatgpt.com/docs/dots/tasks-and-memory
- https://learn.chatgpt.com/docs/environments/cloud-environments

## 迁移前的现有架构与位置

当前是“服务器承载网站，本地电脑执行论文”的 pull 模式：

用户上传 PDF → 网站保存任务 → 本地 worker 通过 HTTPS 领取 → 本地科学流程 → 两份中文 Word 和分任务代码/结果打包 → 上传网站 → 用户下载。

### 本地电脑

| 内容 | 位置或名称 |
| --- | --- |
| 源码与当前未提交改动 | `D:\耿同学agent` |
| 仓库执行政策 | `D:\耿同学agent\AGENTS.md` |
| GitHub | `https://github.com/li6666-del/geng-agent` |
| 本地 worker 计划任务 | `GengAgent-LocalWorker` |
| worker 身份 | `geng-local-main` |
| worker 配置 | `D:\geng-artifacts\tmp\online-0926\worker.json` |
| 案例根目录 | `C:\Users\李松尚\Desktop\耿同学agent_cases` |
| worker 日志 | `D:\geng-artifacts\logs\online-worker-*.stderr.log` 及同名 stdout 日志 |
| 模型配置 | `D:\耿同学agent\configs\models.deepseek-v4.1-flash.json` |
| 共享科学 Python | `D:\geng-artifacts\shared-python\science-py312\Scripts\python.exe` |
| 本地周清理任务 | `GengAgent-WeeklyCleanup`，周一北京时间 04:10，错过后可补执行 |
| 开发与检查记录 | `D:\geng-artifacts\reports`、`logs`、`tmp`、`patches` |

worker 配置含认证信息，仅在已授权的本机操作中使用；不要打印完整配置、复制到交接文本或提交 Git。模型密钥也不写入报告或源码。本文件只交代位置，没有包含口令、token 或私钥。

`docs/local_web_worker.md` 说明通用运行方式；其中“未配置自动开机启动”的通用描述不适用于本机已安装的 `GengAgent-LocalWorker` 计划任务。管理前读取现有任务，不重复安装。

### 网站服务器

服务器历史地址为 `207.57.126.43`，网站域名在阿里云管理，服务器来自野草云。下列配置来自 `deploy/web/` 与已有部署记录，首次接手要通过已授权连接复核。

| 内容 | 位置或名称 |
| --- | --- |
| 网站 systemd 服务 | `geng-agent-web.service` |
| 当前部署入口 | `/opt/geng-agent-web/current` |
| 保留的发布目录 | `/opt/geng-agent-web/releases/` |
| 网站 Python | `/opt/geng-agent-web/venv/bin/python` |
| 服务运行用户 | `geng-web` |
| Web 后端监听 | `127.0.0.1:8768`，通过 Nginx 对外提供 HTTPS |
| 网站环境配置 | `/etc/geng-agent-web.env`，含秘密配置，仅在授权环境中使用 |
| 网站数据库 | `/var/lib/geng-agent-web/web.db`（SQLite） |
| 网站案例文件 | `/var/lib/geng-agent-web/cases` |
| 周清理 | `geng-agent-cleanup.timer` / `.service`，周一北京时间 04:00 左右 |

源码中 `tools/remote_web.sh` 是可选工具，不是确认当前线上服务启动方式的依据；线上先读取真实 systemd 单元和 `current` 指向。不要使用本地开发仓库替换服务器账号数据库或论文数据。

## 接手与迁移前先完成的检查

1. 阅读仓库 `AGENTS.md`，读取当前 Git 状态。2026-10-07 本机有大量未提交的后端改动，不能用 GitHub 上的 main 覆盖本地文件；正式测试与科学运行按项目政策在本地进行。
2. 为 Dot 准备并发布包含本项目准确版本的 Codex Cloud 环境。临时连接本机可用于读取尚未同步的源码和交接资料，但不作为迁移后的运行依赖。
3. 检查公开健康接口、现有 worker 计划任务、真实进程、最新日志及对应案例状态。不要仅凭计划任务显示 Running 判断论文正在执行。
4. 通过 Dot 已有的授权方式验证服务器连接，先只读检查本站 service、部署目录、数据库和清理 timer。若没有服务器授权，明确向用户说明缺少哪个连接入口；不要声称已接管服务器。
5. 将实际能管理的范围、云环境准备情况、尚缺的访问能力和发现的问题回报给用户，按上述步骤完成迁移验证。

本地只读检查示例：

```powershell
Get-ScheduledTask -TaskName 'GengAgent-LocalWorker','GengAgent-WeeklyCleanup'
Get-ScheduledTaskInfo -TaskName 'GengAgent-LocalWorker'
```

服务器只读检查示例（已授权 SSH 会话内）：

```sh
systemctl status geng-agent-web.service --no-pager
systemctl status geng-agent-cleanup.timer --no-pager
readlink -f /opt/geng-agent-web/current
journalctl -u geng-agent-web.service -n 60 --no-pager
```

日志可能含用户文件名或业务信息，向用户汇报时只摘取诊断需要的部分，秘密值不得输出。

## 判断任务是否真正交付

本地网站案例一般为 `cloud_<case_id>`；清理后重新运行的代次可为 `cloud_<generation>`。保留案例根的 `.cloud-worker.json` 与案例根目录下的 `.cloud-worker` 身份记录。

本地完成上传的关键证据是 `.cloud-worker.json` 中：

- `pipeline_complete` 为 true；
- `uploaded_job_id` 与本次 job_id 一致；
- `remote_status` 为 succeeded。

单独存在 pipeline_complete 或 Word 文件，不能说明网站交付结束。若发现失败，区分工程失败与科学未复现，保留现场后按已有恢复路径处理。

最终下载包结构是：

```text
复现交付包/
  论文复现结果对比报告.docx
  本地复现报告.docx
  复现任务/
    t01/
      代码/
      复现结果/
      readme.md
    t02/...
```

任务目录名称以实际映射为准，不保证只有两项，也不能强行重新编号。`readme.md` 应说明每个代码和结果文件的内容与用途。

## 当前需保留的工程决策与记录

- 宿主负责传递、执行和记录，科学判断交给智能体；不要为了网站运维重新加入智能体输出的语义判决或结构拦截。
- 10 月 5 日已修复合法中文源码在采样边界被误漏进 Reporter 快照的问题；报告编辑器与最终打包共用真实目录映射。121 项相关离线回归通过，真实 MR-NOMA 交付的 89 个文件路径与内容保持一致。证据：`D:\geng-artifacts\reports\fix1005a-summary.json`、`fix1005a-actual.json`。
- 上述修复未改写历史已交付报告。MR-NOMA 运行记录及已知限制见 `D:\geng-artifacts\reports\mrmon1005-issues.md`。
- 单次运行暂时没有新输出不等于卡死；Windows 下进程记录的 output_bytes 可能直到进程退出才更新。结合阶段、工具产物、真实子进程和日志判断。
- 一个网站案例根只运行一个接单 worker；当前逐篇论文执行，论文内部并发使用既有科学流程设置。不要为排队问题启动身份相同的第二套 worker。

## Dot 接入说明与本机访问的用途

官方文档说明：在 Dot 个人资料的 Computers 中，为 Your computer 选择 Allow access，并完成连接确认。连接后 Dot 可以创建本地 Work/Codex 任务或继续已有本地 Codex 任务。使用本机时，这台电脑需要在线且桌面应用保持打开；Dot 的电脑连接与 Codex 的电脑连接是独立权限。

官方依据：

- https://learn.chatgpt.com/docs/dots/computers-and-apps
- https://learn.chatgpt.com/docs/dots/getting-started

以上本机连接用于迁移前读取本地资料或维护旧运行。目标运行路径是 Dot 调度已配置的 Codex Cloud 环境；云端闭环完成后，新论文复现应可在本机离线时继续。当前尚未执行这一迁移，不能把 Dot 已在线理解为项目已经搬到云端。
