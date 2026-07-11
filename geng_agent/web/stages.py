from __future__ import annotations

# 面向用户的流水线步骤（合并 Word 导出为「报告」一步）
DISPLAY_STAGES: list[tuple[str, str]] = [
    ("paper", "论文解析"),
    ("paper_memory", "论文记忆"),
    ("engineering_facts", "工程事实"),
    ("paper_thesis", "论文主张"),
    ("repro_tasks", "复现任务"),
    ("experiment_index", "实验索引"),
    ("repro_project_manifest", "复现清单"),
    ("repro_project", "复现代码"),
    ("runtime", "运行复现"),
    ("reproduction_report", "本地复现报告"),
    ("result_review", "论文对比报告"),
    ("review", "主审查报告"),
]


def stage_label(stage_id: str) -> str:
    for sid, label in DISPLAY_STAGES:
        if sid == stage_id:
            return label
    return stage_id


def build_stage_progress(inspect: dict) -> list[dict]:
    by_name = {item["stage"]: item for item in inspect.get("stages", []) if isinstance(item, dict)}
    next_stage = inspect.get("next_stage")
    complete = next_stage is None

    rows: list[dict] = []
    for stage_id, label in DISPLAY_STAGES:
        info = by_name.get(stage_id, {})
        ok = bool(info.get("ok"))
        if complete:
            state = "done" if ok else "skipped"
        elif stage_id == next_stage:
            state = "running"
        elif ok:
            state = "done"
        else:
            state = "pending"
        rows.append(
            {
                "id": stage_id,
                "label": label,
                "state": state,
                "ok": ok,
                "reason": info.get("reason"),
            }
        )
    return rows
