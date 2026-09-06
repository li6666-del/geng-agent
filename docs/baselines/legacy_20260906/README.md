# 历史 case 观测基线

本目录由当前 `python -m geng_agent benchmark` 只读汇总 OWC、OTFS 和 DeepSC-S 三个已有 case。原 case 未修改，也没有作为新版运行结果使用。

- 三个 case 的代码版本、任务覆盖和结束阶段不同，不能据此比较新版成功率或速度。
- 旧日志能确认 69 次 Codex 调用，但无法重建完整 token 数或金额；`unknown` 不能当作零成本。
- OWC 的 23.411 秒来自旧文件的最后一次运行记录，不是整个复现耗时。
- 历史科学终态按原记录列出；缺少新版终态或独立标签的任务保留为未评估。传统 `Matched/Failed` 列不能替代独立的 `Scientific outcomes` 表。
- 当前小样本盲评、真实证据切片和工程闭环验证见上两级目录的 `remediation_validation_20260906.md`。

后续比较应固定论文集合、计算预算与验收标签，保存每次版本及完整运行账本，并将标签与生成、审查代理隔离。
