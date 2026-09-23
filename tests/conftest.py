"""Offline boundaries for the pre-supervisor phase unit tests.

These suites test the original phase adapters using incomplete mock documents.
The run controller has its own integration tests in test_supervisor_*.py; it must
not accidentally start a paid model from an older phase-only fixture.
"""
import pytest


@pytest.fixture(autouse=True)
def offline_capability_dispatch(request, monkeypatch):
    # Legacy suites isolate professional owner behavior. Do not accidentally
    # invoke a paid scheduling model merely because their adapter now has tools.
    if request.module.__name__.split(".")[-1] == "test_supervisor_tools":
        return
    from geng_agent.supervisor import RunSupervisor
    from tests.supervisor_decisions import choose_tools
    original = RunSupervisor._request
    def request_decision(self, node_id, **kwargs):
        if kwargs.get("trigger") == "tool_dispatch":
            return choose_tools(kwargs["context"])
        return original(self, node_id, **kwargs)
    monkeypatch.setattr(RunSupervisor, "_request", request_decision)


_PHASE_UNIT_MODULES = {
    "test_agent_consolidation", "test_case_paths",
    "test_delivery_quality_cost", "test_model_config_scope", "test_moderator_foundation_recovery",
    "test_multimodal_extraction", "test_paper_thesis", "test_pipeline",
    "test_run_cost_and_determinism", "test_task_concurrency_policy",
    "test_upstream_prompt_contracts", "test_workflow_version",
}


@pytest.fixture(autouse=True)
def phase_unit_orchestration_boundary(request, monkeypatch):
    if request.module.__name__.split(".")[-1] not in _PHASE_UNIT_MODULES:
        return

    def run_phases(*, context, analyze, execute, report, finish_analysis, **_kwargs):
        analysis = analyze()
        if context.options.analysis_only:
            return finish_analysis(analysis)
        return report(analysis, execute(analysis))

    monkeypatch.setattr("geng_agent.pipeline.run_supervised_pipeline", run_phases)
