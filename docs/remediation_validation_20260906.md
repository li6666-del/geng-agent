# 通用复现整改：实施与验证记录

本轮按用户要求在 Windows 本地验证。`AGENTS.md` 已移除远端执行/验证前置条件，远端脚本保留为可选工具。现有未提交改动作为实施基线保留，没有提交 Git 或改写外部论文 case。

## 五组改动与验证边界

| 改动 | 主要实现 | 直接验证 |
| --- | --- | --- |
| 科学信息与裁决 | `semantic_merge.py`、`pipeline_analysis_flow.py`、`verification_result.py`、`task_reporter_validation.py` | 符号/小数/指数/Unicode 不碰撞；完整任务规格替换；新反证不丢弃；缺失本地支持证据不能变为成功 |
| 运行与源码、配置、结果关联 | `execution_receipts.py`、`execution_client.py`、`execution_sandbox.py`、`task_writer_runner.py` | 真实 OS 沙箱子进程；旧 CSV/no-op 拒绝；确定性同字节新运行通过；smoke/full 混淆拒绝；非零返回保留；源码、检查点、环境变更失效；已有 pyc 不绕过源码绑定 |
| 共享范围与定点修订 | `foundation_scope.py`、`foundation_revision.py`、`execution_plan.py`、`agentic_foundation.py` | 私有源码可以修改；多 binding 依赖不丢失；共享训练有生产者；修订保留旧快照，只发布有关共享模块的新版本 |
| 局部恢复与停止 | `writer_lineage.py`、`task_writer_state.py`、`pipeline_execution_flow.py` | 单元级依赖；补包保留沙箱与无关结果；旧凭证不正向复用；重试比较真实源码/配置/数值变化；仅更改评论不能延长循环 |
| 独立交付与质量/成本基线 | `delivery_environment.py`、`environment_rebuild.py`、`delivery_evidence.py`、`codex_cost.py`、`benchmark_quality.py` | 实际新建无 system-site-packages 的 venv；搬移后运行；原执行字节可追溯；中断/恢复成本不清零；分离规则回归、独立盲评与历史观察 |

## 实际执行的检查

- 本机解释器：`C:\Users\84475\miniconda3\envs\torch\python.exe`；Python 3.11.15、pytest 9.0.3。`python -m geng_agent doctor` 成功，必需编排依赖和常用科学包可用。
- 默认桌面沙箱曾阻止 Windows 临时目录写入。相同 pytest 命令在获准的本地进程权限下正常执行；没有通过改测试规避这类权限错误。
- `tests/test_delivery_end_to_end.py` 实际运行 Codex 子进程适配器、Writer 请求客户端、受约束科学子进程、独立 Reporter 工作区、报告终态注入、打包和新虚拟环境 smoke。外部代码生成与语义判断由确定性 fake CLI 代替，科学执行和环境运行没有 mock。这证明工程闭环，不证明真实 LLM 在未知论文上正确实现算法。
- `tests/test_observed_scientific_runtime.py` 实际进行 PyTorch 梯度训练、保存共享检查点并由另一个任务加载评价；参数确有变化，消费的检查点与生产凭据摘要一致，改变训练代码后拒绝复用。采用真实 OS 沙箱，未 mock PyTorch 或科学进程。
- `tests/test_execution_sandbox.py` 通过 Python 和原生 `CreateFileW` 分别验证：输出和运行缓存可写，源码与宿主 audit 不可写；多层缓存目录也可创建。真实训练曾暴露启动器 HOME 与科学 HOME 混用的问题，现已在可信启动代码内分离，未扩大写范围或改动全局配置。
- 额外实际执行从选定解释器导出 `numpy==2.4.3`，在全新、不共享 system-site-packages 的 venv 从记录的 PyPI 来源安装，再运行复高斯信道的速率随 SNR 增长 smoke。**安装、版本核对和计算均通过，27.703 秒**；完整记录见 `third_party_numpy_validation_20260906.json`。这项检查含真实第三方依赖下载，仍不代表已验证全部大型训练环境。
- 新增取消回归真实启动长实验后取消，验证沙箱启动器与科学子进程一起结束，没有继续产出“完成”数据。独立复查另补了新增数值产物、三级生产链、修订期间补包失败的回归；普通报告图片不独立触发昂贵重跑，也不能独自充当新科学证据。
- 共享产物另核验三级生产链及执行时的实际依赖身份：升级或删除 `commpy` 对应的 `scikit-commpy` 会使消费者失效，无关包和独立任务仍可复用；smoke 链可用于 smoke，不能晋升为 full 证据。运行与缓存恢复都执行这项检查，不增加科学子进程。
- `git diff --check` 无内容错误；仅提示本机 Git 的 LF/CRLF 换行约定。

## 独立校准与历史基线

八个合成案例覆盖信道样本形状、估计方法排序、优化精度、学习方法身份，输入与预设标签分别保存在 `tests/fixtures/scientific_calibration/cases.json` 和 `quality_baseline.json`。另一个无历史上下文的评审只读取输入，将判断先写入 `docs/blind_calibration_independent_20260906.json`，主线程随后才对照标签。**8/8 科学终态、8/8 重跑判断一致**。这是小样本、明确证据条件下的判据校准，不能外推为整个系统 100% 成功率。

本次盲评使用协作子代理，未经过项目的 Codex CLI 计费账本；其精确模型标识、独立调用 token 数和净评审耗时没有可靠导出，均视为未知，不并入项目的零成本样本。下一轮实际 Reporter 基线应同时保存这些运行元数据和原始回答。

`docs/scientific_calibration_observation_20260906.md` 则记录未读取旧 Reporter 裁决的真实 OTFS 证据切片：普通约 2.34 倍偏差不单独重跑；核心近似关系约 4.13 倍偏差仍不能接受；仅两次 realization 且宽置信区间的排序冲突需要保留统计不确定性。这里没有虚构已定位的代码原因。

另实际执行 `python -m geng_agent benchmark <OWC case> <OTFS case> <DeepSC-S case> --out docs/baselines/legacy_20260906`，只读汇总三个已有 case。输出是**历史观测基线**：版本不同、任务覆盖不同、旧日志缺少完整用量。尤其 OWC 旧文件里的 23.411 秒和零 token 不是整个复现成本，不能用于宣称新版更快。没有预设独立标签的历史任务保持未评估。

## 新机制的成本与尚未证明的效果

- 运行凭证增加流式文件哈希、进程前后环境清单和旧输出归档；环境探测耗时单独记录。其价值是拒绝旧结果、错配置和过期检查点，不额外重跑一遍 full。
- 干净环境首次安装可能下载依赖，之后按环境身份复用安装缓存，每份新包仍执行 smoke。安装失败会公开，不能通过复用宿主包伪装第三方可运行。
- 共享科学修订只串行化修订发布，不把所有科学任务串行执行。Checkpoint 保留不等于有效：消费时需要生产者凭证与当前依赖对应。
- 科学子进程同时使用 OS 写隔离与既有 Python 文件/环境保护。Windows 当前后端允许系统级读取，不能把 Python 读取保护说成完整的原生读取隔离；Linux 后端本轮未实机验证。沙箱命令不调用模型，缺少必要本地能力时不静默取消隔离。
- 本轮没有重新训练全部真实论文，也未得到新版多论文端到端成功率、GPU 训练吞吐或金额节省的统计对照。真实模型能力仍需后续用未见论文和固定预算测量；不能把 fake CLI 工程测试算作这种实证。

暂不继续按文件行数拆分模块，也不增加像素拟合、小数值偏差重试或层层 JSON 门禁。下一轮最有价值的实证是固定预算下运行未见论文，使用预先隔离的独立标签测量科学误判、无效重跑和完整交付率。

## 统一全套结果

实现冻结后在仓库根目录实际运行：

```powershell
python -m pytest -q -ra --disable-warnings --tb=short -p no:cacheprovider
```

最终结果：**760 passed, 15 skipped, 1 warning, 170 subtests passed in 188.68s (0:03:08)**。

15 项跳过均来自 Windows 当前符号链接创建权限或仅适用于 POSIX 的进程组、目录权限和属主语义；不能把它们计为验证通过。真实 Windows 原生写隔离、科学训练、进程树取消、恢复和干净环境交付检查均实际执行。最后 `git diff --check` 通过。
