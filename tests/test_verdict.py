from geng_agent.pipeline_verification import build_terminal_review_summary
from geng_agent.verification_result import aggregate_task_verifications


def test_no_host_aggregate_scientific_verdict_or_confidence():
    notes = [{"task_id": "t", "outcome": "reproduced", "host_action": "complete"}]
    result = build_terminal_review_summary(aggregate_task_verifications(notes))
    assert result["writer_summary_result"]["verification_result"]["tasks"] == notes
    for summary in (result, result["writer_summary_result"], result["writer_review_document"]):
        assert not {"verdict", "confidence", "overall_alignment", "overall_result_credibility"} & summary.keys()
