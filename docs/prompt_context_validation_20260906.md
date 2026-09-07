# 提示词与上下文整改：实施及验证记录

基线为 `9399d5c`。本轮完成五组提示词与调用上下文整改，所有验证在本地进行；没有提交或推送，没有启动完整论文复现。既有论文 case 保持只读。修改计划见 [prompt_context_remediation_plan.md](prompt_context_remediation_plan.md)。

## 实现范围

| 改动 | 最终行为与主要位置 | 保留的质量保障 |
| --- | --- | --- |
| 1. 科学纠错与裁决边界 | `analysis_repair.py`、`pipeline_json_stage.py`、`agentic_analysis.py` 区分格式修复、带原始证据的科学纠错、恢复误改字段；`verification_result.py` 支持有原论文证据的验收依据争议、纠正及不适用 | 完整候选和原始科学基准保留；沿用原尝试预算；争议不能清除独立方法反证；不跨单位/指标/区间强算比例 |
| 2. 实际输入与缓存身份 | `codex_runner.py` 保存最终 stdin、附件身份、不可变调用清单和完整压缩 transcript；`llm.py` 保存实际请求 body；`prompt_identity.py`、`runtime_status.py`、各角色缓存纳入实际契约、模型、附件及必要宿主策略 | 不记录 Authorization；已有尾部日志仍可快速查看；科学字段保留，只排除已知审计元数据和传输换行噪声；无关任务仍按局部依赖复用 |
| 3. 恢复与执行职责 | Foundation 恢复旧 delivery 和具体错误；Writer 只携带当前反馈和恢复原因，用哈希定位同版证据；`execution_client.py`/`execution_receipts.py` 提供 `--submit`/`--status`，宿主管理在途进程 | 旧源码、checkpoint、观察凭据保留；同版相同请求才合并；不同配置、输入、源码不能取得旧结果；准备模式由宿主拒绝 full；环境请求满足后才归档 |
| 4. 科学投影与证据范围 | `analysis_prompt_context.py` 去已知重复并保留未解决状态及不同搜索结果；回补重新搜索全部已加载文本；Writer 保留相关完整实验条目；Reporter 将自述与独立证据分开，去重复任务/事实副本 | 保留公式条件、方法身份、张量/复数约定、单位、归一化、训练评价口径及完整原文入口；API 明确记录文本降级与未见图；Reporter 附件清单反映实际可见范围 |
| 5. 报告事实与质量/成本验证 | Editor 使用独立终态、已核实事实、来源基址和宿主计数；`tools/prepare_prompt_context_cases.py`、`tools/prompt_context_benchmark.py` 构建和执行固定证据三组对照 | 执行与产物有效不称为科学支持；缺失次数不记零；正文不能覆盖宿主终态；评分标签不进入 worker 目录；未用退出码或 schema 通过率代替科学质量 |

新增字段均为现有证据 note 的可选表达，导出的两个 verification schema 同步更新，没有新增严格 JSON 门禁。修复不增加默认模型尝试预算。

## 工程验证

最终冻结源码全套回归：**821 passed，15 skipped，170 subtests passed**，328.98 秒，退出码 0。15 个跳过项为 10 项 Windows 符号链接权限限制和 5 项 POSIX 专属语义；不把跳过项算作通过。有 1 条既有 Starlette/httpx 弃用警告，未为此更换依赖。`git diff --check` 通过，HEAD 仍为 `9399d5c`。

实际运行命令：

```powershell
$env:GENG_CODEX_CMD = '__geng_test_no_live_llm__'
& C:\Users\84475\miniconda3\envs\torch\python.exe -B -m pytest -p no:cacheprovider --basetemp "$env:TEMP\geng-prompt-context-final-tests" --junitxml "$env:TEMP\geng-prompt-context-final-tests.xml" -q
```

关键反例覆盖以下行为，包含真实小型宿主子进程测试，不只检查提示词文字：

- Codex/API 三轮修复：先出现 global/override 冲突，再合法修正 scope 但误改 normalization，最后恢复 normalization，保留合法 scope；同一 task 的多个 experiment 可以重排。
- API JSON 输出格式回退保留图片；不支持图片时明确记录文本降级；未知网络/auth 错误不伪装成文本成功。
- 错单位、错误目标和非论文验收条件不能强迫错误比较；原有 `supported:false` 和独立方法反证不能被依据争议覆盖。
- 在途 smoke/full、配置路径及内容、checkpoint、源码变化不能混用凭据；队列缓存建立后发生变化，以及执行实际读取但未显式声明的数据变化也需重新核验。
- Writer 恢复可读取同版审查文件；已满足或平台不适用的环境请求可安全消费，未满足请求保留。
- prompt/schema、模型、附件、宿主执行说明和裁决实现改变会使相关缓存失效；纯审计变化、传输换行以及无关任务/环境扩充不扩大重算。
- 末次执行有效但科学未复现时，报告同时准确表达两者；未知历史运行次数不推断。
- 对照工具保持原始科学证据和任务事实不变，不泄露评分标签，拒绝逃逸 worker 目录的路径，读取统计对同一工具调用只计一次。

首轮全套集成发现两类问题：verification 导出 schema 尚未同步，以及一个测试错误假定任何很小的示例去重后都必须更短。后者移除了错误的长度不变量，继续核验科学字段和原文完整性；真实成本由下面的对照测量决定。

早期一个旧 salvage 测试因缓存契约变化且缺少 mock，意外进入了真实 Codex 入口；是否完成网络调用及其用量无法从残留记录确定，不能记为零。该测试已使用当前缓存 envelope 并强制 mock；此后集成默认使用不存在的 Codex 命令，防止测试遗漏 mock 时意外发起调用。下列 21 次是另行明确授权、完整留有调用凭据的实验，不包含这一未知尝试。

## 已执行的 21 次静态模型对照

用户明确授权固定论文文本、生成源码和结果观察发送到本机配置的 Codex 模型服务。实际执行 7 个片段 × 3 组，共 21 次，无自动模型重试；模型 `gpt-5.6-sol`，reasoning effort `xhigh`，并发 2。调用壁钟跨度 601.47 秒。未执行论文代码、安装依赖或修改历史 case。

三组为：

1. **基线**：从 `9399d5c` 取 Reporter 的科学指令。
2. **修改后指令**：采用本轮 Reporter 科学指令，与基线使用完全相同的文件证据。
3. **修改后指令＋去重**：相同科学指令，仅应用生产中的 `_canonicalize_reporter_paper_evidence` 文件去重。

各组共用一个只读静态片段回答合同，不使用完整项目报告交付合同。因此这是 Reporter 科学判断与上下文消融，不能代表整条 pipeline 的成功率，也不能证明 Writer 和上游提示改动的模型质量已经提高。历史数据包装重建了旧 bundle 的重复结构，并非恢复了每一次历史调用的全部隐藏输入。三组均明确没有图片。

模型输出及原始标签均保留。实验过程中和结束后未改标签重跑。比较完成后，另修正了生产 Reporter 的附件可见性措辞及宿主清单；这项说明性变化有工程回归，未追加模型对照。对照的科学指令快照和 SHA256 保存在实验目录。

### 科学判断

| 片段 | 来源 | 证据要求及实际判断（各组相同） | 允许修复重跑 |
| --- | --- | --- | --- |
| OWC observable | 真实论文、生成源码及 summary | CP 投影改变了被比较的 observable；不是仅有峰形差异，判 unsupported | 是，有具体实现改正 |
| OTFS finite/DE | 真实论文、CSV、smoke 配置 | 35 dBm 约 4.13 倍差异不能支持论文明确的紧近似；未定位代码原因，判 unsupported | 否 |
| OTFS MSP/SSP | 真实论文、2 次实现 CSV、smoke 配置 | 样本均值未建立预期排序；不能宣称已证明总体期望反转，判 unsupported | 否 |
| DeepSC-S 缺运行产物 | 真实论文分析材料 | 缺 checkpoint、评价数据、测量和凭据；判 unassessable，不能把缺证据说成已证明未训练 | 否 |
| 普通 4 倍数值差异 | 明确标为合成的既有 fixture | 不单独否决方法与主要结论，判 supported | 否 |
| 论文明确精度未达标 | 合成 fixture | 明确精度要求失败，且有错误停止条件的因果证据，判 unsupported | 是 |
| 学习方法被解析曲线替代 | 合成 fixture | 排序相似不证明训练方法身份，判 unsupported | 是 |

原始严格标签评分各组 **6/7**；差异全部出现在 OTFS 两次实现的排序问题。独立子代理在未读取预设标签、评分或模型回答的情况下检查了两个 OTFS 和 DeepSC 包：该排序题问的是“现有结果是否建立优势”，`unsupported` 是合适答案；若问总体期望的真实排序，才更适合 `unassessable_missing_information`。三组回答都明确没有证明期望反转，没有擅自归因或要求重跑。

因此按明确的所问命题核验，三组为 **7/7**；这不是三组所有题的独立盲标注结果，盲评范围为上述 3 个历史片段。保留原始 6/7 和命题澄清，不能把标签歧义隐藏成模型改进。

每组历史片段 4 个、合成片段 3 个。每组错误成功判断 **0/7**，不必要重跑 **0/7**，重跑决策差错 **0/7**。这些有限样本没有显示新指令比基线更准确，只说明本轮没有观察到科学判断退化。

### 实际用量与读取成本

以下为 CLI 完整 transcript 报告的 token 用量；不是按字符估算。输入包括多轮上下文和工具交互。缓存输入是输入的子集，不能再相加；无法据此精确还原全部重复输入 token。

| 指标，7 次调用合计 | 基线 | 修改后指令 | 修改后指令＋去重 |
| --- | ---: | ---: | ---: |
| 输入 token | 772,994 | 697,515 | 614,459 |
| 其中缓存输入 | 681,088 | 592,256 | 487,040 |
| 未缓存输入 | 91,906 | 105,259 | 127,419 |
| 输出 token | 12,820 | 12,778 | 11,345 |
| 总 token | 785,814 | 710,293 | 625,804 |
| 工具调用 | 72 | 62 | 58 |
| 工具结果字符 | 281,283 | 309,741 | 205,689 |
| workspace 初始文件字节合计 | 196,188 | 196,188 | 148,407 |
| 各调用耗时之和，秒 | 432.6 | 384.4 | 352.6 |

相对相同新指令的未去重组，去重组本次输入 token 减少 **11.9%**，读取工具调用从 62 到 58。相对基线，输入 token 减少 **20.5%**，工具调用从 72 到 58。但未缓存输入分别增加约 **21.1%**、**38.6%**，所以**不能声称费用下降**。使用同一账号、仅一次样本、前缀缓存及执行顺序均可能影响结果；各 case 虽轮换三组顺序，仍不足以消除混杂。

按 case 检查也有读取反例：OTFS tight-approximation 从新指令组 3 次工具调用增为去重组 10 次；学习方法合成例从 3 次变为 9 次。不能把合计减少解释为每篇论文都会更便宜。21 次调用均有用量与完整 transcript，21 份保存的 prompt 与记录哈希全部一致，回答中的证据路径均能解析到各自 workspace 的真实文件；路径存在本身不代替科学核验。不推算账号订阅的美元价格。

## 上游与 Foundation 的离线输入测量

对 OWC 历史完整 brief 保留原指令和论文块，仅替换 task/fact、resolution、ledger 的确定性投影，得到以下字符量。它们不是新一轮模型调用或真实 token 数据。

| 阶段 | 原始字符 | 投影后 | 减少 |
| --- | ---: | ---: | ---: |
| 回补后刷新 | 341,923 | 262,058 | 23.4% |
| 最终任务定稿 | 463,868 | 319,538 | 31.1% |
| 科学架构 | 394,812 | 392,165 | 0.7% |

同一历史输入经 Foundation builder 测得 60,415 → 49,530 字符。四个 singleton 当前 builder 分别为 84,541 / 139,820 / 133,065 / 150,991 字符；其中一个因保留完整相关实验条目略增。没有为了压长度删除科学字段。以上不能推出费用、耗时或科学成功率的改善。

## 可复查产物及复跑

原始实验目录：`C:\Users\84475\AppData\Local\Temp\geng-prompt-context-validation`。包含：

- `dataset.json`：7 个固定片段、原始预设标签、11 条来源读取记录（10 个不同原始文件）的 SHA256；历史文件结束后逐一复核，全部未变化。
- `comparison/plan.json`：模型比较范围、基线 revision、实际科学指令哈希及局限。
- `comparison/workers/`：21 个独立只读输入目录，目录名不泄露案例标签；没有预设答案和来源标签文件。
- `comparison/audit/`：逐调用最终 prompt、附件清单、完整压缩 transcript、用量事件及模型回答。
- `comparison/results/`、`comparison/summary.json`：保留原始标签评分与模型论证。

另有持久归档：`C:\Users\84475\Desktop\耿同学agent_cases\prompt_context_validation_20260906\comparison_evidence.zip`，389 个文件，1,004,677 字节；已逐项验证 ZIP CRC。SHA256 为 `061abd98ae55f7d8fe4621a591070926c8b691f094d74e471f54fc53c22e523a`。清单中原始绝对路径保留实际运行时位置，解包后按归档根重定位；没有改写原始审计证据。

准备或检查计划无需模型调用；只有显式 `--run` 才调用模型：

```powershell
& C:\Users\84475\miniconda3\envs\torch\python.exe -B tools/prepare_prompt_context_cases.py --cases-root 'C:\Users\84475\Desktop\耿同学agent_cases' --out "$env:TEMP\new-prompt-comparison\dataset.json"
& C:\Users\84475\miniconda3\envs\torch\python.exe -B tools/prompt_context_benchmark.py "$env:TEMP\new-prompt-comparison\dataset.json" --out "$env:TEMP\new-prompt-comparison\preflight" --context-ablation
```

再次模型比较需选新的输出目录，在第二条命令增加 `--run --workers 2`；本轮没有重复执行。比较工具不是生产门禁，不阻止正常论文复现。

## 已知边界与后续验证

- 保留完整论文和相关公式、科学契约、小型可调用 API 文档、关键训练产物与谱系、独立 Reporter、宿主执行凭据；不继续按文件大小盲删。
- 缓存格式及角色契约已改变，首次恢复旧 case 会重新判断相关缓存；旧源码和产物保留。新指令本身要求重新审查时，不能承诺零模型调用。
- API 回退和上游/Writer 恢复以本地反例及 transport mock 验证，没有对每个远端供应商开展真实兼容性实验。
- 没有运行新的完整 OWC、OTFS 或学习型论文复现，因此不能宣称端到端成功率、训练质量或交付实验的科学结论已经改善。
- 后续若继续投入模型预算，先固定无歧义命题及独立标签，再对失败片段做多 seed 的交叉顺序重复；分开记录错误成功、无依据重跑、漏读、方法身份、趋势/排序/精度、实际调用及缓存用量。学习类补一个真实 checkpoint 训练/评价链，优化类补约束与收敛日志，统计仿真类补成对实现与不确定性。无需现在重跑全部历史 case。
