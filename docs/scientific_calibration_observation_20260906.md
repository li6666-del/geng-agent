# 科学判据校准：真实运行的独立证据切片裁决

日期：2026-09-06。审阅对象：`case_twc_otfs_multisat_20260719_023016`，论文为 *Joint Precoding and Link Scheduling for OTFS-Based Multi-Satellite Cooperative Transmission*。

这是一次单审阅者、隐藏既有裁决标签的实际证据审阅。审阅者先读论文提取段落、原始 CSV、生成源码和配置，再记录下面的判断；没有读取本 case 的 Reporter verification、risk_report、task_agent_result、任务验收结论或最终报告，也没有拿本轮作者编写的 reference_reporter_note 充当盲评。case 文件只读，没有执行或修改其生成项目。

本记录**不是**当前完整 pipeline 的端到端运行，不提供成功率、盲评准确率或重跑成本改善的统计结论；审阅者参与了本轮判据实现，因此也不是对整改方案身份盲法的对照试验。这里保留真实观察与判断，用于检查判据是否会混淆不同失败模式，后续应另用未见论文、独立评审者和实际 Reporter 调用评估。

## 事先采用的判断原则

逐项区分核心方法/趋势/比较关系与普通数值差异。小于十倍的普通差异不独立授权重跑；排序反转、趋势错误及论文明确强调的近似精度可构成实质问题。缺乏足够统计或实验条件的观察不能升级为确定错误。发现实质问题与授权 Writer 重跑是两个判断：后者还需要可检验的原因、代码目标和有价值的复验。

## 实际判断

| 证据切片 | 原始观察 | 本次独立判断 | 最小后续动作与价值 |
| --- | --- | --- | --- |
| Fig.4：JPL 迭代单调性 | `p13_c1` 说明接受的迭代应使和速率不下降。CSV 中 SSP 的接受序列为 0, 6.7243, 13.3623, 18.1152, 22.6258, 23.5293, 24.7556, 25.6131, 26.6426；MSP 也从 0 单调增加至 25.4298。 | **在观察到的运行中支持这项趋势**。无需因曲线终值或迭代数外形差异重跑。这不等于任务整体已复现：CSV 只显示两个 regularization 分量，而论文该图条件为四颗卫星，未核实运行命令与完整配置对应。 | 报告限定支持范围；若要宣称论文配置的收敛效果，先核对实际执行配置，不直接重训或遍历种子。 |
| Fig.5：高功率方案排序 | `p13_c1` 主张高功率 MSP 比 SSP 更有优势、WMMSE 在比较中最好。50 dBm 的 CSV 为 SSP=63.1377、MSP=39.0784、WMMSE=70.7116、CMMT=137.4693，全部只有 2 次 realization；SSP 记录的 95% CI 半宽达 39.5830，MSP 为 5.0740。smoke 配置只用 4 个 DD modes，且限制搜索候选。 | **当前证据不足以支持论文排序，观察到的是实质比较关系冲突，不能用“小于十倍”消解**。同时，宽区间和约简计算不足以证明完整算法的期望排序已经反转；本切片保留为 `unassessable`，不臆造已定位的方法 bug。 | 首先核对成对原始 realization、实际 profile 和候选约简是否改变比较公平性。只有确定成本可控、能消除这项不确定性后才增加样本或恢复必要维度。 |
| Fig.8：普通图上数值读数 | 20 dBm 的本地 JPL=3.4172，单独标明的图上 ES reference=8，比例约 2.34；同一点 JPL 大于 CL=3.2588 和 JHU=2.7954。源码把 paper reference 独立标注为非计算结果，并在小规模审计中另计算 ES。 | **这项绝对幅度差不独立构成重跑理由**。也不能反向利用纸上 ES 数值证明本地已接近穷举最优；本地 Search benchmark 与真正完整 ES 的界限必须保留。 | 保留数值差和配置假设。验证 near-optimal 需要相同条件下独立的上界/可枚举 ES 证据，而不是朝图上参考值调参。 |
| Fig.8：DE 与有限维计算的紧密近似 | `p15_c1` 明确主张两条曲线在适中维度几乎重合。20 dBm 的有限维值=3.4172、DE=129.9946，比例 38.04；35 dBm 的有限维值=59.9044、DE=247.6069，比例 **4.133**。smoke 配置仍列 Q=8×8、M=128、N=4，但只取 1 次 MC、2 对 quadrature。 | **当前数据不支持这项核心近似结论**；35 dBm 即使小于十倍也不能视为普通幅度偏差。保留 `unsupported` 观察，但不宣称已证明哪个公式实现错误，也不据此自动授予 Writer 重跑。论文没有给出数值百分比阈值，故不虚构 1% 门槛。 | 先做同一 channel/normalization 下的有限维-DE 对照与 quadrature/MC 小规模收敛诊断，定位近似条件、实现或数值积分问题后再给具体修订目标。这一步有辨别原因的价值，重复原配置重跑没有同等价值。 |
| Fig.9：复杂度趋势与人为常数 | 源码 `_count_all` 为 LRZF 计算分步运算量，却对四种 baseline 使用渐近表达式乘配置常数；CSV 清楚标为 assumed proxy。固定其它参数时 LRZF 从 M=16 的 17,672,339,456 到 M=32 的 35,344,678,912 恰为两倍，ZF-PMO 为八倍。论文同时讨论随 M 的增长率与小 M 时的交叉关系。 | **线性对三次增长的该项模型趋势有依据；具体 crossover 或绝对优越性不能仅凭自由常数证明**。这不是要求精确重绘计数，而是区分已计算的算法成本与阶数代理能够支持的结论范围。 | 报告代理常数的假设与敏感性；若 crossover 属于核心结论，再读取基线原算法推导一致的计数口径。没有核心依赖时，不值得为绝对计数实现所有大型基线。 |

以上均为**切片级科学判断**，不据此给整个任务赋 `reproduced` 终态。此记录没有足够证据为任何一项指定已确认的 Writer 代码修订，所以没有把发现问题直接转换为重跑指令。

## 对本轮实现的检验与局限

此次真实观察表明，普通数值差、核心近似关系、排序证据和统计不确定性可以给出不同结论；尤其不能用统一十倍阈值忽略 Fig.8 的近似失效，也不能因 Fig.5 反向均值而忽略只有两次采样的事实。已有规则回归另覆盖“明确要求 1% 而只达到 2%”的成对反例；它是合成回归，**本次真实切片未遇到具有明确百分比门槛的案例**，不能说这项边界已得到独立实证。

新增 `tests/fixtures/scientific_calibration/cases.json` 与 `quality_baseline.json` 将模型输入和预设标签分开，为后续独立 Reporter 盲评准备输入；自动测试使用 reference note，只验证宿主归一化，不把这项规则测试计为模型盲评。

## 证据定位与完整性

本地 case 根目录：`C:\Users\84475\Desktop\耿同学agent_cases\case_twc_otfs_multisat_20260719_023016`。以下均相对该根目录；它们是已有外部 case 的来源定位，不是可随仓库独立运行的测试依赖。

| 文件 | SHA-256 |
| --- | --- |
| `paper_chunks.json`，主要 `p13_c1`、`p15_c1` | `c0582699cad80d25fe0db801bd3ff31e5634872e202eeb471d625406e84a418f` |
| `repro_project/outputs/reproduce_fig_4_jpl_convergence/results.csv` | `7636059fc3658d80d349358d73d10f44a89d3db4a9e0c584a32a499337f482bb` |
| `repro_project/outputs/reproduce_fig_5_sum_rate_comparisons/results.csv` | `2dd333b0e721059995184d896e05d363a5653f58461a34bf68cc930fd2e3b15c` |
| `repro_project/outputs/reproduce_fig_8_jpl_scheduling/results.csv` | `f3f1ca4372323c2182d9bb18d465f8549f53c8b224a238bdb1418e7d6d77d6bb` |
| `repro_project/configs/reproduce_fig_5_sum_rate_comparisons_config_smoke.json` | `4fd55250bf8798ca645f0922dbe0986c036103c2a2dc8102cd51064d0b2c4b45` |
| `repro_project/configs/reproduce_fig_8_jpl_scheduling_config_smoke.json` | `371977ce71f3de6188b40564cea7fd718684581b0170bd064e0c2d785c800f56` |
| `repro_project/tasks/reproduce_fig_8_jpl_scheduling.py` | `92770512a4db2049751196b7960e1faafc676b6a0259daa0ff1653634a8a6161` |
| `repro_project/tasks/reproduce_fig_9_complex_multiplications.py` | `200871dcaf8359a47c2b30f6c936df38e20187f44035fad8ad64c2f379c3a8e1` |
| `repro_project/outputs/reproduce_fig_9_complex_multiplications/results.csv` | `a436940c032963b9b4269b6c0d506d78c382a81b33ea78aadcae2c3bf02cb1bd` |
