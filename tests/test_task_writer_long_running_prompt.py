from __future__ import annotations

from geng_agent.task_writer_prompts import (
    _build_execution_unit_writer_brief,
    _build_task_writer_brief,
)


def _assert_durable_long_run_protocol(prompt: str) -> None:
    flattened = " ".join(prompt.split())
    assert prompt.count("## Host-owned scientific execution") == 1
    assert "--submit" in flattened and "--status" in flattened
    assert "Submission is not completion" in flattened
    assert "host owns the scientific process" in flattened
    assert "wait for an in-flight run" in flattened
    assert "never invent return code 0" in flattened
    assert "Start-Process" not in flattened
    assert "writer_progress/live_runs" not in flattened


def test_single_task_writer_uses_durable_long_run_protocol() -> None:
    prompt = _build_task_writer_brief(
        index=1,
        task={"task_id": "long_task", "figure_or_claim": "Fig. 3"},
        manifest_entry={
            "task_id": "long_task",
            "module": "long_task",
            "output_subdir": "long_task",
        },
        facts={"engineering_facts": []},
        experiment_index={"experiments": []},
        paper={"chunks": []},
        paper_context_json="",
        paper_thesis=None,
        run_repro=True,
    )

    _assert_durable_long_run_protocol(prompt)


def test_compound_writer_uses_same_durable_long_run_protocol() -> None:
    unit = {
        "unit_id": "unit_long_pair",
        "mode": "compound",
        "task_ids": ["producer", "consumer"],
        "relationships": [],
        "dependencies": [],
        "artifact_ids": [],
    }
    members = [
        (
            1,
            {"task_id": "producer"},
            {
                "task_id": "producer",
                "module": "producer",
                "output_subdir": "producer",
            },
        ),
        (
            2,
            {"task_id": "consumer"},
            {
                "task_id": "consumer",
                "module": "consumer",
                "output_subdir": "consumer",
            },
        ),
    ]
    prompt = _build_execution_unit_writer_brief(
        unit=unit,
        members=members,
        facts={"engineering_facts": []},
        experiment_index={"experiments": []},
        paper_context_json="",
        paper_thesis=None,
        bindings={},
        run_repro=True,
        review_feedback={},
        foundation_enabled=False,
        case_runtime=None,
    )

    _assert_durable_long_run_protocol(prompt)
