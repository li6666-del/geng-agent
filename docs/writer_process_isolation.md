# Windows Writer 进程隔离

这次修改只有两项：限制 Writer 对其他进程的控制权，并提供只停止自己实验的工具。没有增加科学内容、任务措辞或输出格式的审核；没有增加自动重启、看门狗或 GPU 排队。

## 权限边界

每个 Writer 会话建立唯一 Windows AppContainer 身份。Codex CLI、它启动的 PowerShell/Python、科学执行、环境探测和私有环境安装都在其中。子进程继承隔离。宿主在恢复运行前固定可写目录，不能让旧环境的目录链接扩大授权范围；可信安装运行库仅授予只读权限。

启动器先挂起新进程，确认 Windows 身份与完整性级别，绑定本次 Job，再开始执行。每次科学执行有独立 Job；停止它不会结束 Writer CLI 或其他实验。科学主进程退出后关闭本次 Job，避免遗留子进程进入下一次运行。隔离建立失败时不会以宿主身份执行 Writer。

这种边界防止直接结束、注入和接管外部进程；它不承诺解决内存耗尽、机器断电或操作系统漏洞。资源调度不在本次修改范围内。

## 实验联网

正式实验默认直连。宿主在整理实验环境后设置 `NO_PROXY=*` 和 `no_proxy=*`，不继承桌面的代理变量，避免 Python `urllib` 回退读取 Windows 系统代理而连接隔离外的本机代理服务。实验启动的子进程正常继承这些设置；实验进程与文件权限不因此改变，也不增加本机回环例外。模型调用的 `proxy_mode` 仍单独生效。

这是默认联网路线，不是网络访问禁令。确实需要代理的下载可在实验代码使用的客户端中显式配置隔离可达的代理，并按该客户端要求覆盖默认绕过设置；宿主不自动切换代理或额外重试。自行清空环境变量的子进程、以及不遵循这些变量的下载工具，需要在其调用处明确配置。

## 初次 Windows 设置

当前 Codex 使用 Windows 盘符规范化 API。AppContainer 默认无法查询盘符到卷的映射，即使任务目录本身已可读写，Codex 仍可能启动时报 `Access is denied`。这与智能体输出或论文内容无关。

`tools/setup_writer_appcontainer_access.py` 提供一次性兼容设置：仅向本项目的只读能力 SID 开放对象命名空间与卷管理器的必要查询权限，不授予终止进程、写文件或管理卷的权限。默认只检查；应用操作需要管理员权限。脚本不自动提权，保留变更记录并支持撤销自身授权。具体命令以脚本 `--help` 为准。

本机在管理员 PowerShell 中执行：

```powershell
& 'D:\geng-artifacts\shared-python\science-py312\Scripts\python.exe' 'D:\耿同学agent\tools\setup_writer_appcontainer_access.py' --apply
```

手动运行记录保存在 `D:\geng-artifacts\reports`；用相同命令的 `--rollback <apply记录路径>` 撤销本次授权。Windows 重启可能重新创建这些内核对象。日常使用可安装下述固定开机恢复任务，避免每次重启后手动应用；Writer 本身不会自动提升权限。

### 开机自动恢复

首次在管理员 PowerShell 执行一次：

```powershell
& 'D:\耿同学agent\tools\install_writer_boundary_startup.ps1' -Action Install
```

安装器创建 `GengAgent-WriterBoundaryStartup` 计划任务：开机时由 SYSTEM 静默执行，不需要登录、密码或再次确认；安装后也立即运行一次。任务只给同一项目能力 SID 恢复固定的 5 项查询授权，已有授权时不改动。启动时盘符暂不可用可间隔一分钟重试两次，不常驻、不轮询实验、不启动 Worker 或模型。

实际执行文件位于 `C:\Program Files\GengAgentWriterBoundary\WriterVolumeQuery.exe`（以 Windows 实际 Program Files 路径为准）。安装目录和任务定义仅允许 SYSTEM/管理员修改；普通用户只有读取权限。该程序由系统 .NET 编译为独立程序，开机不加载项目源码、共享 Python、用户配置或任意外部命令。这样不会把普通用户可改写的仓库变成 SYSTEM 执行入口。Writer 的 AppContainer 和进程隔离保持不变。

无需管理员权限的状态查询：

```powershell
& 'D:\耿同学agent\tools\install_writer_boundary_startup.ps1' -Action Status
```

运行记录保存在受保护安装目录的 `logs/latest.json` 和历史 JSON；开发验收报告另存 `D:\geng-artifacts\reports`。如果升级恢复程序，重新以管理员运行 Install；仓库修改不会自动改变高权限任务。

管理员执行 `-Action Uninstall` 可取消今后的自动恢复，保留程序与日志，不删除现场。它不会立即撤销当次开机已授予的权限；如需撤销，可在卸载后使用手动设置工具的既有 `--rollback` 记录。

不能用关闭 AppContainer、扩大 Everyone 权限或开放个人 `.codex` 目录来绕过这一步。部署后应完成真实 CLI 的离线探针，而不只看 Python 单元测试通过。

当前 Python 的 `mkdir(0700)` 还存在遗漏容器 SID 的上游兼容问题。项目仅在 Writer 的私有环境安装一个兼容模块，将当前容器 SID 加到原有的私有目录权限中；共享 Python 不变。该模块不在容器中运行时不改变任何行为。虚拟环境先创建目录，再启用兼容模块并用 Python 自带 wheel 初始化 pip，后续安装仍由 Writer 的隔离进程执行。

模型目录文件（`model_catalog_json`，例如 DeepSeek 的模型能力配置）可能由另一个本地账号安装，宿主能读取但无权修改其 ACL。Writer 启动时先将文件内容复制到本次 `audit/prompt_inputs/<调用编号>/model_catalog.json`，再授权该调用目录，并让 CLI 读取副本；原文件的所有者和权限不变。权限异常会标明具体路径及所请求的访问位，便于区分目录权限、模型配置权限和盘符查询问题。

## Writer 停止自己的运行

宿主在创建本次科学运行的 `stdout.log` 和 `stderr.log` 后，只给该 Writer 的容器身份授予这两个文件的只读权限；`run_task.py --task T1 --status` 返回各自路径。Writer 因而可以查看当前任务运行中的完整日志和此前本会话的运行日志，诊断失败与续跑。不会授权整个 `audit/execution_runs` 目录、旁边的审计文件或其他 Writer 的日志。文件权限随该 Writer 容器域关闭一并撤销；非 Windows 执行仍由其原有沙箱负责。

```text
python run_task.py --task T1 --config configs/T1.json --mode full --device cpu --submit
python run_task.py --task T1 --status
python run_task.py --task T1 --stop <status 中的 run_id>
```

工具使用宿主持有的本次进程/Job 句柄，不把运行编号当作 PID。其他 Writer 的编号、未知编号和错误任务不会停止进程；已经结束的旧编号不会影响新的运行。停止后保留日志、已有结果和中断收据，Writer 可以修改后再次提交。

## 验证边界

- `tests/test_windows_process_boundary.py`：真实 Windows 进程、PowerShell 和子孙进程隔离。
- `tests/test_owned_execution_stop.py`：停止自己的实验、其他实验继续、停止后再次执行。
- `tests/test_writer_boundary_dispatch.py`：各入口使用隔离启动器，失败不回退。这里使用替身，不证明实际 OS 隔离。
- `tools/probe_writer_process_boundary.py`：真实 Codex CLI，固定本地 Responses 响应与测试专用进程；不调用付费模型。系统兼容设置未完成时，启动失败必须如实保留，不能写成端到端通过。
- `tests/test_writer_startup_helper.py`：用系统 .NET 编译独立恢复程序；验证只补缺失权限、保留其他 ACE、拒绝冲突 deny、重复执行不扩权，且固定目标/SID 与宿主读取逻辑一致。测试只读系统权限。

所有测试产物放在 `D:\geng-artifacts`。这些工程验证不等同于真实论文复现，也不会恢复此前中断的论文任务。

### 本机部署验收（2026-09-27）

管理员配置已应用，并通过只读复查。配置后的真实进程隔离与拥有者停止测试共 10 项通过；正式入口相关回归 51 项及 5 个子测试通过。Codex 桌面版的安装目录可能是目录链接，入口现在先解析宿主选定的可执行文件，再向其真实安装目录授予只读权限。

真实 Codex CLI 在隔离身份内完成了固定本地 Responses 服务的两轮交互，并实际调用 PowerShell/Python：可以读写自己的文件，但获取宿主测试进程和另一个 Writer 测试进程的终止权限均失败，Windows 返回错误 5，两个测试进程保持存活。项目 `run_codex_subprocess` 的直接执行和带主持人进程记录两条路径也均完成工具调用、最后消息和审计文件回传。

本地模拟服务需要 Windows 调试用回环例外；此次只给临时测试身份添加，结束后已撤销并复查。生产 Writer 没有增加回环例外。另用全新隔离身份直接与 `api.deepseek.com:443` 完成 TLS 1.3 握手及证书校验；未发送 HTTP 请求或凭据。

完整记录：`D:\geng-artifacts\reports\writer-isolation-acceptance-20260927.md`。这些测试验证启动、权限和信息传递，不验证模型判断或论文复现效果；没有实际模型调用。后续开机自动恢复的安装和触发验收单独记录，不将手动触发等同于已实际重启电脑验证。
