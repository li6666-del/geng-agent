"""Host-rendered terminal facts, independent of an Editor's prose and omissions."""
from __future__ import annotations

from pathlib import Path
import re
from typing import Any

BEGIN = "<!-- geng-terminal-facts:start -->"
END = "<!-- geng-terminal-facts:end -->"
LABELS = {"reproduced": "已复现", "reproduced_with_assumptions": "带假设复现",
          "not_reproduced": "未复现", "inconclusive_missing_information": "信息不足",
          "execution_failed": "执行失败"}


def _cell(value: Any) -> str:
    return str(value or "未提供").replace("|", "\\|").replace("\n", " ").replace("\r", " ").replace("<", "&lt;").replace(">", "&gt;")


def terminal_fact_block(packets: list[dict[str, Any]]) -> str:
    lines = [BEGIN, "## 任务终态与核验记录", "",
             "以下事实由宿主从独立审查记录直接生成；正文解释不改变这些终态。", "",
             "| 任务 | 复现目标 | 科学终态 | 核心结论观察 |", "| --- | --- | --- | --- |"]
    for packet in packets:
        task = packet.get("task") or {}
        verification = packet.get("verification") or {}
        criteria = verification.get("core_conclusions") or []
        observed = "; ".join(f"{item.get('claim_id', '')}: {item.get('status', 'unassessable')}"
                             for item in criteria if isinstance(item, dict))
        outcome = str(packet.get("terminal_outcome") or verification.get("outcome") or "unclassified_terminal_result")
        lines.append("| " + " | ".join(_cell(x) for x in (packet.get("task_id"), task.get("figure_or_claim") or task.get("title"),
                                                         LABELS.get(outcome, outcome), observed)) + " |")
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
        rendered = block + "\n" + cleaned + "\n"
        if rendered != body:
            path.write_text(rendered, encoding="utf-8")
            changed.append(name)
    return changed
