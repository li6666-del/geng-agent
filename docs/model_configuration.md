# 全项目统一模型配置

项目用 Codex CLI 承担文件读写和工具调用，由一份全局配置选择模型服务。一次运行中的事实分析、Foundation、Task Writer、Reporter、异常主持人和报告编辑器共用同一个服务商、模型、推理强度及凭据来源。智能体职责与工作区仍独立，模型选择不再按角色分配。

## 使用方法

从 `configs/models.example.json` 复制一份自己的配置。文件中的 `default` 选择全局 profile；例如改为 `deepseek` 后，所有角色都使用该配置。示例默认仍是 GPT。

先离线检查（不读取服务商密钥、不发送网络请求、不启动论文）：

```powershell
python -m geng_agent models --config D:\settings\models.json
```

运行时显式选择：

```powershell
python -m geng_agent review paper.pdf --out case_001 --run-repro --model-config D:\settings\models.json
```

也可通过进程环境变量 `GENG_MODEL_CONFIG` 指定配置文件。显式 `--model-config` 优先。Python 调用使用 `ReviewPipeline.run(..., model_config_path=Path(...))`，`run_stage` 同样支持。

切换模型只需修改 `default`，或选用另一份配置文件。`profiles` 保存可选配置，一次只启用其中一种。旧版 `roles` 字段已取消，加载时会提示删除，不会静默混用不同模型。离线 `models` 命令也只显示一份全局配置。

DeepSeek V4.1 Flash 的现成配置为 `configs/models.deepseek-v4.1-flash.json`，使用正式 API 标识 `deepseek-flash`、最高推理档 `max` 和随项目固定的官方模型目录 `configs/deepseek-models.json`。先在启动进程中提供 `DEEPSEEK_API_KEY`，再把这份文件传给 `--model-config`。密钥不能写入 JSON。

项目自带及示例模型配置均把 `web_search` 设为 `live`，选中的同一模型供所有角色按需使用 Codex 网页搜索。事实分析、规划、Writer、Reporter、主持人和报告编辑器也都获得其职责范围内的命令行出站网络能力；网络查询并非每一步的必做动作。Writer 仍在独立 Windows AppContainer 内运行，Reporter 和报告编辑器仍只写各自工作区；主持人和不交付文件的分析调用采用继承只读文件权限的网络配置，不因此获得文件写权限。原论文和本地执行记录的证据地位不因外部查询而改变。

命令行网络使用 Codex 的 `sandbox_workspace_write.network_access` 或只读网络权限配置；[Codex 配置参考](https://learn.chatgpt.com/docs/config-file/config-reference)和[权限配置](https://learn.chatgpt.com/docs/permissions)分别说明两者。只读网络配置需要支持自定义权限配置的 Codex CLI；本地 0.153.4 的无模型探针确认该配置可解析、写入受拒，且能连接 `api.deepseek.com:443`。第三方模型对 Codex 托管 `web_search=live` 的实际支持及权限配置在完整模型调用中的表现尚未用付费调用验证；如果服务商拒绝搜索工具，需依据真实调用错误处理，不能把配置声明当作已通过的网页搜索实测。

正式科学实验也允许出站网络，Windows AppContainer 与其他系统的科学沙箱仍保留各自的进程和文件边界，宿主收据如实记录 `network_enabled=true`。本机使用真正的隔离科学进程，免费完成了到 `api.deepseek.com:443` 的 TCP/TLS 1.3 握手；没有发送 HTTP 请求或调用模型。网络权限不自动提供 API 密钥：科学进程继续使用去除模型凭据的最小环境。若论文算法本身需要调用付费模型，仍需另行设计该实验的凭据交付；不要把已开放网络误写成已完成在线模型实验。Writer 使用联网获取的科学输入或模型响应时，应在任务内保存实际消费的数据及来源，供后续独立审查。

这份 DeepSeek 配置及示例中的 DeepSeek profile 默认使用 `proxy_mode: "direct"`。论文理解、规划、Writer、Reporter、主持人和报告编辑器统一直连：仅在各智能体子进程中移除继承的 HTTP/HTTPS/ALL_PROXY（不区分大小写），并设置 NO_PROXY 为 `*`。不修改系统代理、浏览器设置或宿主环境。Writer 的 AppContainer 进程隔离保持启用，直连无需向其开放本机代理的回环访问。配置在运行开始时冻结；已有运行不热切换、不重启。

## 配置字段

顶层字段为 `schema_version: 1`、`default`、`profiles`。profile 名称是自定义的小写标识。未知字段、重复 JSON 字段和不存在的 profile 引用都会报配置错误。

| profile 字段 | 含义 |
| --- | --- |
| `provider` | 服务商标识；`openai` 无端点和密钥引用时使用 Codex 内建认证，其余走项目显式配置 |
| `model` | 服务商接受的具体模型名称 |
| `base_url` | 自定义服务的接口根地址；只接受 HTTP(S)，不允许在地址中附凭据或查询参数 |
| `env_key` | 存放 API 密钥的环境变量**名称**；不能写密钥本身 |
| `wire_api` | 当前实现只接受 `responses` |
| `proxy_mode` | `direct` 为智能体进程直连；`inherit` 继承启动环境的代理设置。省略时为 `inherit`，项目自带 DeepSeek 配置显式选择 `direct` |
| `reasoning_effort` | 模型接受的推理等级；省略、`null` 或 `omit` 表示项目不显式覆盖 Codex 的模型推理设置；`none` 是实际发送的等级，与 `omit` 不同 |
| `supported_reasoning_efforts` | 可选的受支持等级列表；设置后会拒绝列表之外的显式推理等级 |
| `supports_images` | 是否允许该模型接收阶段图片，默认 `true` |
| `supports_tools` | 是否支持工具调用，默认 `true`；项目智能体必须具备此能力 |
| `supports_json_schema` | 是否允许 Codex 的 `--output-schema`，默认 `true`；不等于能输出普通 JSON |
| `model_catalog_json` | 可选的 Codex 模型目录文件；相对路径以这份配置文件所在目录为基准 |
| `web_search` | 可选 `disabled`、`cached`、`live`；`live` 提供实时网页搜索，实际工具可用性仍取决于所选服务商与模型 |

这些能力标记是配置声明，不是实际能力测试。当前主要智能体通过工作文件交付结构化结果，不要求 `--output-schema`；因此第三方示例保守地将该项设为 `false`。将来调用方要求此参数时会在启动前说明不兼容，不能靠吞掉参数继续运行。

### Codex 模型目录与版本

模型目录告诉 Codex 该模型采用哪些工具格式、输入模态、上下文窗口和默认推理等级。项目的能力标记负责本地检查，**不会自动改写 Codex 的模型目录**。第三方模型应按供应商当前的 Codex 接入说明准备目录，并通过 `model_catalog_json` 显式指定；单纯修改项目模型名不能替代这一步。目录文件内容哈希进入配置身份，运行途中被修改时拒绝继续使用旧快照。

`omit` 仅表示本项目不设置推理覆盖值，不能保证 Codex 最终请求不含 reasoning：Codex 仍可能采用模型目录的默认值。不要把 GPT 的 `medium` 原样套到不支持它的模型上。

文件配置模式要求 CLI 同时支持 `--ephemeral` 和 `--ignore-user-config`。项目对单次 Worker 跳过个人 `config.toml`，指定模型与 provider，并禁止命令包装中再附带 profile、config、model 或模型功能覆盖。个人配置文件不会被修改，内建 OpenAI 认证仍可使用 Codex 自己的凭据。

Windows 的 Task Writer 使用项目建立的独立 AppContainer，覆盖 Codex 与所有后续进程。只有外层 Windows 隔离已经建立，内部 CLI 才使用 `danger-full-access`，避免再次切换沙箱账户；它不表示获得宿主权限，也没有关闭外层隔离的失败回退。其他 Worker 继续显式设置 `windows.sandbox = "elevated"`。初次部署的 Windows 路径查询兼容设置见 [Writer 进程隔离](writer_process_isolation.md)。自定义服务商仍使用 `env_key` 和 `requires_openai_auth = false`，不设置会影响全局登录状态的 `forced_login_method`。

Worker 不继承祖先项目的 Codex 配置；如果其工作目录自身存在 `.codex/config.toml`，则明确停止该次调用。系统和组织级约束仍由 Codex 执行。个人配置中的 MCP/插件等扩展不会随文件配置自动启用，当前项目依赖的是 Codex 基础文件和命令工具；需要额外扩展时应另行显式设计其配置与身份记录。

## 凭据和故障

在启动项目的环境中设置所选服务商对应的凭据，例如 `DEEPSEEK_API_KEY` 或 `ZAI_API_KEY`。不要把密钥填入 JSON、命令参数或版本库。示例中的 GLM 地址是 Z.AI 的 Responses 入口，不能直接套用国内智谱平台的密钥和套餐。

文件配置模式只为 Codex 进程传入所选服务商凭据，工具进程采用精简环境并保留 Python、运行目录和宿主执行代理所需变量。凭据值不进入模型配置快照、调用命令或正常审计；CLI 错误输出中的所选凭据也会按值脱敏。科学代码执行继续使用原有隔离环境。

缺少密钥、不支持图片或工具、不支持输出 schema、CLI 版本不足、配置冲突等均作为调用/配置故障处理，不转成“论文未复现”，也不自动换服务商、删图片或替换科研验收规则。

## 运行快照、缓存与费用

每次 `ReviewPipeline.run` 开始时解析一次不可变模型配置，所有阶段和并发线程共享同一对象。运行中修改配置文件或环境中的模型名，不会改变该次运行。凭据在调用前读取，允许独立轮换；凭据值不参与缓存身份。

- `audit/model_configs/<run_id>.json` 保存每次运行的唯一模型配置；`audit/model_config.json` 是最新快照。新版快照 `schema_version: 2` 使用单个 `config` 字段，旧历史记录保留原样。
- 调用的 `input.json`、Worker 状态和用量事件保存对应模型身份。
- 运行快照的 `transport`、调用输入及状态的 `transport_config` 单独记录代理模式。连接路线不参与科研缓存身份，改为直连不会使已完成的论文理解和规划失效。
- 服务商、端点摘要、模型、推理设置、能力声明和模型目录内容均参与模型身份，供分析、Foundation、Writer、Reporter 和 Editor 现有缓存机制使用。
- 恢复运行重新解析当前配置；相关阶段的缓存按身份重新核对，不会强行套用上一次选择，也不删除原始运行记录。
- 用量继续记录 Codex 返回的 token 数据，金额没有可靠计价依据时保持未知，不把第三方 API 费用视为零。

## 旧配置兼容

没有选择配置文件时，仅从全局 `GENG_CODEX_MODEL` 和 `GENG_CODEX_REASONING_EFFORT` 读取选择，默认 GPT-6 / medium。旧的 `GENG_CODEX_<ROLE>_MODEL` 与 `GENG_CODEX_<ROLE>_REASONING_EFFORT` 已不再读取。未知模型在未显式设置推理等级时，不自动套用 medium。单次 Worker 接口也不再接受推理强度覆盖。

旧模式仍继承 Codex 自身的 provider 配置，记录中标为 `managed: false`、`provider: inherited`，不声称已经完整识别个人配置。需要多服务商隔离与准确缓存身份时使用新的文件配置模式。选中配置文件后，不再叠加旧 `GENG_CODEX_MODEL` 和推理强度环境变量，避免无意混用。

`--model`、`--api-key`、`--base-url` 和 `GENG_LLM_*` 仍属于旧 `--analysis-backend llm` 分支，不控制 Codex 全流程。选择项目统一配置时禁止同时使用此旧分支，避免分析阶段偷偷使用另一套模型；冲突会在创建或清理案例产物前报错。

## 验证范围

离线测试检查配置解析、参数生成、身份差异、缓存、线程隔离、凭据边界和原流程回归。`tools/probe_model_flow.py` 提供主动触发的真实验证：检查所选账号的模型列表、Responses API、Codex 工具的实际 Python 命令执行与文件产物，再通过 `--run-flow` 运行一个有已知答案的极小 BPSK 合成案例。仅凭模型进程返回成功或写出文件不能通过连接检查。输出必须在项目外；可用 `--prompt-key` 在终端隐藏输入凭据。此脚本会产生 API 费用，不在测试或普通启动时自动运行。它不构成真实论文科研能力认证。

模型目录来源是 DeepSeek 官方 Codex 安装脚本 v1.3.0；只保留 `deepseek-flash` 条目，删除与 `model_messages.instructions_template` 重复的旧 `base_instructions`。未执行安装脚本，也未修改用户级 Codex 配置。

配置示例依据 2026-09-17 核对的官方资料：[Codex 自定义 provider](https://learn.chatgpt.com/docs/config-file/config-advanced)、[DeepSeek Codex 接入](https://api-docs.deepseek.com/quick_start/agent_integrations/codex/)、[Z.AI 工具接入](https://docs.z.ai/devpack/tool/others)、[GLM-5.3-Flash 模态说明](https://docs.z.ai/guides/vlm/glm-5.3-flash)。示例用于接线，不能据此宣称对应模型已通过本项目的端到端验证。

### 2026-09-17 接入层历史验证记录

以下是统一模型改造前的历史结果；2026-09-18 的当前配置与真实 DeepSeek 实测见[统一模型实测记录](unified_model_deepseek_validation_20260918.md)。

- 新配置、传输边界、作用域、CLI、模型身份，以及分析、Foundation、Writer、Reporter、Editor、依赖任务和费用相关回归集：**334 项通过、141 个子测试通过、7 项跳过**。跳过项均涉及当前 Windows 无法创建符号链接的权限限制。
- 随后补充修复单阶段恢复入口：先验证并冻结模型配置，再清理旧阶段产物；错误配置保留已有产物，清理期间改文件不改变已选模型。该定向回归 **35 项通过、2 个子测试通过**，与上述集合存在重复，不累加报告数量。
- 实际运行 `python -m geng_agent models --config configs/models.example.json`，离线输出五个角色的预期配置；没有读取第三方凭据或发送请求。
- 另行运行的 `test_delivery_end_to_end` 原生沙箱集成测试失败，执行收据报 `Could not find home directory`。在内存中加载 Git HEAD 的修改前 `codex_runner.py` 替换执行器后复测，仍是相同错误；本次没有修改沙箱实现、放宽隔离或将该测试改成跳过。该环境问题仍未解决，不宣称整套端到端检查通过。

全部测试使用已有 Python 环境，临时输出位于项目外，禁用 pytest 缓存和 Python 字节码写入；未安装依赖、调用付费模型或运行真实论文复现。
