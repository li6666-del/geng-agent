"""Chinese report wording; scientific records and technical identifiers stay intact."""
from __future__ import annotations


CHINESE_REPORT_RULES = """## 报告语言要求
最终的本地复现报告和结果对比报告均使用简体中文，包括任务标题、正文、结论、图表说明、差异、判决理由、假设和不确定性。
所有进入报告的自然语言字段必须使用中文：report_title、report_explanation、decision_reason、comparison_summary、differences、non_material_differences、remaining_uncertainties、asset_notes、verified_facts.text、core_conclusions.local_observation、basis_review.reason、comparison_reason、unavailable_reason，以及 name/regime 等字段中的自然语言说明。
不要因为论文、代码或此前记录是英文，就照搬英文段落作为报告正文。忠实转述为中文，不增加或改变科学判断、事实、数值、比较条件或不确定性。结束前自行检查这些字段的语言。
JSON 字段名、枚举值、稳定 ID、公式、变量、单位、文件路径和代码标识保持协议原样；BER、SNR、GPU 等通用缩写及算法/库的专有名称可保留。必须引用的论文原句和原始错误日志可以保留英文，但要明确标为原文并给出中文解释。
语言和排版修订不构成科学重跑理由，不新增科研判断或实验。
"""
