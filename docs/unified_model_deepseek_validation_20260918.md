# 统一模型改造与 DeepSeek 实测

日期：2026-09-18。项目目录：`D:\耿同学agent`。

## 改造结果

- 一个运行只选择一个服务商、模型、推理强度及凭据来源，所有智能体共享同一个不可变配置对象。
- 删除 `roles` 配置及单次 Worker 推理强度覆盖；旧角色环境变量不再参与模型选择。
- CLI 显示一份统一配置。运行快照升级为 `schema_version: 2` 的单个 `config` 字段；历史记录保留。
- 统一配置不能与旧 `analysis_backend=llm` 分支混用；冲突检查发生在案例创建或阶段清理之前。
- 保留智能体职责、独立工作区、缓存身份、并发上下文和执行证据绑定。
- 提供 `configs/models.deepseek-v4.1-flash.json`，选择 `deepseek-flash` / `max`；目录来自 DeepSeek 官方 Codex 脚本 v1.3.0。

## 离线检查

统一配置、传输层、并发作用域、CLI、缓存身份，以及分析、Foundation、Writer、Reporter、Editor、执行单元与费用回归：**242 项通过、7 项跳过、66 个子测试通过**。

第一次宽范围运行中，有两个旧测试以 Windows GBK 默认编码读取 UTF-8 文件而失败。以 `-X utf8` 重跑相同集合后全部通过；未修改这些旧测试或科研验收规则。跳过项是当前 Windows 的符号链接权限限制。

随后针对旧分析入口混用保护与最终修改复测：**89 项通过、52 个子测试通过**（与前述集合重叠，不相加）。真实执行探针的负向检查 **4 项通过**：只写文件、命令失败、修改探针均不能冒充执行成功。

此前在外层受限环境中失败的 `test_delivery_end_to_end.py`，本轮在正常本机宿主上重跑 **1 项通过**。测试中的科学代码仍使用项目原生隔离；模型部分为模拟，不能据此认定 DeepSeek 全流程已成功。

## 真实连接及修正

全部调用使用用户提供的 DeepSeek 密钥，经终端隐藏输入，只在进程内临时保留；未写入配置、源码或实验记录。

1. `deepseek_unified_20260918_01`：账号模型列表返回 `deepseek-flash` 和 `deepseek-v4-pro`；Responses API 在 `max` 下正确返回 `CONNECTION_OK`，87 tokens。Codex 因 `forced_login_method=api` 与已有 ChatGPT 认证冲突，在启动前失败并提示退出登录。
2. 修复：删除强制登录设置；自定义服务商直接以自己的凭据环境变量认证。首轮检查的实际副作用是本机 Codex CLI 已退出 ChatGPT 登录（`codex login status` 确认）；以后如使用订阅认证需重新登录。
3. `deepseek_unified_20260918_02`：Responses API 正常，Codex 模型调用成功，但实际写文件被只读沙箱拒绝。原因是跳过个人配置同时隐藏了 Windows 沙箱设置。
4. 修复：Windows Worker 显式保留 `elevated` 原生沙箱，不关闭隔离。`deepseek_unified_20260918_03` 的模型调用与实际文件创建均通过，随后进入生产流水线。

## 全流程实验

使用 `tools/probe_model_flow.py --run-flow`，材料是明确标记为合成验证用例的 BPSK/AWGN 短文，包含噪声方差与理论 BER 两个闭式计算任务、三个 Eb/N0 点及已知参考值。该验证不等同于真实论文复现能力认证。

案例与完整审计均在桌面 `耿同学agent_cases/deepseek_unified_20260918_03`。修复交付问题后，流程于 **01:26:19** 完成：`runtime_passed=true`、`result_review_passed=true`。两项任务均为 `reproduced`，宿主收据有效；最终项目 `portable=true`，搬移 smoke 实际通过。

分析、Foundation、Task Writer、Reporter、Report Editor 五类角色的实际调用清单均为同一 `deepseek / deepseek-flash / max`。两份正文由该模型编辑器生成简体中文 Markdown，再由现有转换器生成 DOCX；程序未代写报告正文。`result_review.docx` 包含两张本地图，原文无图的限制在正文说明，未伪造原图。两份 DOCX ZIP 完整性检查通过，报告图像引用均位于本地交付目录。

实测中出现 Windows 沙箱初始化取消（1223）、一次登录错误（1326）与一次初始化状态不完整。两个不同版本的 Codex 共用本机沙箱状态是需要进一步验证的可能原因，不写成已确认根因。暂停后检查原生执行，再按检查点恢复；没有关闭生成代码隔离，也没有改科研验收结果。

已完成阶段：材料分析 638.7 秒，任务/架构设计 253.6 秒，共享代码 253.3 秒。共享代码的 9 项生成契约测试实际通过；这些测试由同一模型编写，不能替代独立核验。恢复时前两阶段均命中缓存，没有重复付费分析。

后续验证脚本已加强：连接通过要求观察到 Python 命令成功、特定执行标记、预期文件以及探针源码未修改，不能再以 `apply_patch` 写文件代替执行能力检查；中断也保存已完成记录。

## 实测发现的交付缺陷与修复

1. **子模块漏打包（确定缺陷）**：架构把任务私有实现放在 `src/tasks/noise_variance.py` 与 `src/tasks/theoretical_ber.py`，Writer 正常执行并生成 full 收据。`_writer_package_files` 却在路径任意层级排除名称 `tasks`，导致合并项目缺少 `src/tasks`，迁移 smoke 报 `ModuleNotFoundError: No module named 'src.tasks'`。已改为仅排除由另一路复制的顶层 `tasks/`；新增嵌套包合并回归。未跳过迁移检查。
2. **新增任务测试被当作共享代码修改（确定缺陷）**：提示词只冻结清单所列文件，但宿主只给新 `src/*.py` 放行，没有给新的任务测试放行。BER Writer 新建 `tests/test_theoretical_ber.py` 后被标记 `foundation_modified`，尽管 Reporter 核实实际科学路径的冻结文件哈希未变。已使有明确 scope 的清单同样允许不修改、不覆盖既有模块的新测试，原有冻结文件与导入覆盖防护保留；新增测试同时验证可新增与不可覆盖两种情况。
3. **共享包的纯说明文件冲突（确定缺陷）**：漏包修复后，两个任务的 `src/tasks/__init__.py` 被正确纳入合并，但各自只写了不同的模块说明字符串，原合并器仍按不同代码报冲突。现在只对语法树为空或仅含一个说明字符串的包初始化文件合并说明，原说明分别保存到 `task_notes/package_descriptions`，包说明统一；含导入、赋值、函数等实际代码的冲突仍报错，原执行字节仍保留在执行记录中。这是明确的包装转换，不是重认定原始执行源码。对应打包、执行绑定和交付回归 **50 项通过**。

上述定向回归 **72 项通过、3 项跳过、3 个子测试通过**。第一次使用较长 Windows 临时路径时，一个旧用例复制证据报路径不存在；相同集合换用项目外短路径 `D:\geng-tests-0918-scope-2` 后通过，未修改该用例的预期结果。

修复前审计保存在案例根目录 `before_delivery_fix`；通过已有运行刷新标记让 BER 续作并重新接受核验，没有手工更改旧的通过/失败字段。实际续作没有新增 full：Writer 核对源码、配置、环境和输出哈希后恢复了原 full 产物；宿主按原收据重新验证，第二轮 Reporter 也给出 `run_valid=true`。不能把这次续作写成一次新的 full 执行。噪声方差与 BER 首轮 full 实际执行耗时分别约 3.54 秒和 3.87 秒；两份 CSV 经独立 `math` 公式复算均完全一致（逐点浮点值比较）。最高档模型的处理耗时远高于本例的实际计算耗时。

## 能力结论与剩余边界

- **已验证**：单一 DeepSeek 配置贯穿五类角色；结构化分析与任务设计、共享代码及契约测试、两个并发 Writer、宿主观察的 full 计算、独立 Reporter、中文报告编辑与 Word 交付均实际走过。局部恢复复用了分析、Foundation 和未受影响的噪声方差任务。
- **耗时与用量**：案例记录累计 10 个完整 Codex 智能体会话（每个会话可含多次 API 请求），累计 14,412,421 tokens，其中缓存输入 13,476,992；金额未知。该累计不包含最初两个连接试验、案例外连接检查及被中断而未完整落盘的会话用量，不是账号完整账单。四次流水线调用记录合计约 34.8 分钟，期间排查与修改时间另计。两个 full 数值计算合计约 7.4 秒。
- **仍有报告交接问题**：本地复现报告将完整 full 重运行命令和归档路径映射列为缺口；对比报告也提到 Writer 引用的归档副本未进入 Reporter 的交接快照。已有宿主收据与输出哈希足以完成本次核验，但给编辑器的材料仍不足以写出清晰、可直接照做的完整重运行说明。没有把“报告文件已生成”当作报告内容已无改进空间。
- **未验证**：真实 PDF/MinerU、论文缺口搜索、真实原文图对比、大型仿真、GPU/训练/检查点传递、跨机器依赖重建，以及 GLM 等其他服务商的实际能力。此次材料是合成测试夹具，不能宣传成真实论文复现认证。
- **本机副作用**：首次错误的强制认证参数使 CLI 的 ChatGPT 登录失效；此参数已经删除，若之后要用订阅认证，需重新登录。DeepSeek 凭据只在试验进程中临时存在，结束后已移除。

交付位置：`case/repro_project/`、`case/result_review.md/docx`、`case/reproduction_report.md/docx`。本轮未提交或推送 Git。

## 官方依据

- [DeepSeek V4.1 Flash 发布与 API 名称](https://api-docs.deepseek.com/updates/)
- [DeepSeek Codex 集成与模型目录](https://api-docs.deepseek.com/quick_start/agent_integrations/codex/)
- [Codex Windows 原生沙箱](https://learn.chatgpt.com/docs/windows/windows-sandbox)
