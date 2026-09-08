"""Host-rendered terminal facts, independent of an Editor's prose and omissions."""
from __future__ import annotations

from pathlib import Path
import re
from typing import Any

BEGIN = "<!-- geng-terminal-facts:start -->"
END = "<!-- geng-terminal-facts:end -->"
LABELS = {"reproduced": "已复现", "reproduced_with_assumptions": "带假设复现",
          "not_reproduced": "未复现", "inconclusive_missing_information": "信息不足",
          "execution_failed": "执行失败", "review_incomplete": "审查未完成（工程问题）"}


def _cell(value: Any) -> str:
    return str(value or "未提供").replace("|", "\\|").replace("\n", " ").replace("\r", " ").replace("<", "&lt;").replace(">", "&gt;")


def terminal_fact_block(packets: list[dict[str, Any]]) -> str:
    lines = [BEGIN, "## 任务终态与核验记录", "",
             "以下事实由宿主从独立审查记录直接生成；正文解释不改变这些终态。", "",
             "| 任务 | 复现目标 | Reporter 科学结论 | 核心结论观察 | 直接判决理由 | 工程状态 |", "| --- | --- | --- | --- | --- | --- |"]
    for packet in packets:
        task = packet.get("task") or {}
        verification = packet.get("verification") or {}
        criteria = verification.get("core_conclusions") or []
        observed = "; ".join(f"{item.get('claim_id', '')}: {item.get('status', 'unassessable')}"
                             for item in criteria if isinstance(item, dict))
        outcome = str(packet.get("terminal_outcome") or verification.get("outcome") or "unclassified_terminal_result")
        lines.append("| " + " | ".join(_cell(x) for x in (packet.get("task_id"), task.get("figure_or_claim") or task.get("title"),
                                                         LABELS.get(outcome, outcome), observed, verification.get("decision_reason"), verification.get("engineering_status"))) + " |")
    lines += ["", "科学结论原样来自 Reporter；工程状态未通过时，不将该结论发布为已验证复现。工程问题不等同于论文缺失信息。", ""]
    for packet in packets:
        verification = packet.get("verification") or {}
        issues = verification.get("engineering_issues") or []
        if issues:
            lines.append("- " + _cell(packet.get("task_id")) + " 工程记录：" + _cell("; ".join(map(str, issues))))
    lines += ["", "### 宿主执行记录", "",
              "末次执行与产物有效只记录执行和产物是否有效（0/1，未知留空），不代表支持论文结论；科学支持仅由上方科学终态表达。", "",
              "| 任务 | 有凭据的完整尝试 | 末次执行与产物有效（0/1） |", "| --- | --- | --- |"]
    for packet in packets:
        execution = packet.get("execution_summary") or {}
        def count(key: str) -> str:
            value = execution.get(key)
            return str(value) if isinstance(value, int) and not isinstance(value, bool) else ""
        lines.append("| " + " | ".join((_cell(packet.get("task_id")), count("observed_full_attempt_count"), count("latest_valid_execution_count"))) + " |")
    lines += ["", END, ""]
    return "\n".join(lines)


def publish_terminal_facts(workspace: Path, packets: list[dict[str, Any]]) -> list[str]:
    block = terminal_fact_block(packets)
    changed = []
    pattern = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END), re.DOTALL)
    for name in ("review.md", "reproduction_report.md", "result_review.md"):
        path = workspace / name
        if not path.is_file() or path.is_symlink():
            continue
        body = path.read_text(encoding="utf-8")
        cleaned = pattern.sub("", body).strip()
        image_lines = []
        if name == "result_review.md":
            for packet in packets:
                for key, label in (("local_assets", "本地结果展示"), ("paper_assets", "论文图")):
                    for relative in packet.get(key, []):
                        if relative in cleaned:
                            continue
                        asset_path = workspace / relative
                        if asset_path.is_file() and not asset_path.is_symlink() and asset_path.resolve().is_relative_to(workspace.resolve() / "report_assets"):
                            image_lines += [f"![{_cell(packet.get('task_id'))} {label}]({relative})", ""]
                notes = (packet.get("verification") or {}).get("asset_notes", [])
                image_lines.extend(_cell(note) for note in notes if _cell(note) not in cleaned)
        rendered = (block + "\n" + cleaned + "\n" + "\n".join(image_lines)).rstrip() + "\n"

        if rendered != body:
            path.write_text(rendered, encoding="utf-8")
            changed.append(name)
    return changed
