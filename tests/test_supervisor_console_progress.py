import pytest

from geng_agent.progress import ConsoleProgressReporter


@pytest.mark.parametrize("event", ["supervisor.node_started", "supervisor.node_handoff", "supervisor.blocked"])
def test_console_displays_supervisor_events_and_node_identity(capsys, event):
    ConsoleProgressReporter().emit(event, phase="supervision", message="节点进度",
                                   data={"node_id": "writer:awgn"})
    captured = capsys.readouterr()
    assert event in captured.err and "writer:awgn" in captured.err
    assert "节点进度" in captured.err and not captured.out


@pytest.mark.parametrize(("trigger", "expected"), [
    ("tool_dispatch", "主持人正在安排工具与后续工作"),
])
def test_console_ordinary_moderation_does_not_report_a_failure(capsys, trigger, expected):
    payload = {"decision_id": "one", "trigger": trigger, "action": None}
    original = dict(payload)
    ConsoleProgressReporter().emit("moderator.started", phase="task_reproduction",
                                   message="主持人正在分析异常", data=payload)
    captured = capsys.readouterr()
    assert expected in captured.err and "分析异常" not in captured.err
    assert payload == original


@pytest.mark.parametrize("trigger", ["node_failed", "contextual_check_findings", "stalled_revision"])
def test_console_keeps_actual_incident_message(capsys, trigger):
    ConsoleProgressReporter().emit("moderator.started", phase="task_reproduction",
                                   message="主持人正在分析异常", data={"trigger": trigger})
    assert "主持人正在分析异常" in capsys.readouterr().err


def test_console_keeps_completed_diagnosis_and_existing_step_label(capsys):
    ConsoleProgressReporter().emit("moderator.completed", phase="task_reproduction", step="foundation",
                                   message="缺少必要依赖，交回负责者修复", data={"trigger": "node_failed"})
    captured = capsys.readouterr()
    assert "moderator.completed foundation" in captured.err
    assert "缺少必要依赖，交回负责者修复" in captured.err


def test_console_does_not_expose_unselected_debug_events(capsys):
    ConsoleProgressReporter().emit("debug.payload", phase="supervision", message="internal")
    assert not capsys.readouterr().err
