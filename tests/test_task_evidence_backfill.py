from copy import deepcopy
from geng_agent.task_evidence_backfill import (
    collect_missing_fact_requests, reconcile_final_tasks, summarize_backfill_resolution,
    update_search_ledger, cumulative_resolution_from_ledger,
)


def test_similarly_worded_requests_are_not_silently_merged():
    tasks = {"repro_tasks": [{"task_id": "a", "missing_fact_requests": [{"request_id": "noise_A", "name": "Noise power"}]},
                           {"task_id": "b", "missing_fact_requests": [{"request_id": "noise_B", "name": "noise power"}]}]}
    requests = collect_missing_fact_requests(tasks)
    assert [item["request_id"] for item in requests] == ["noise_A", "noise_B"]
    assert requests[0]["task_ids"] == ["a"]


def test_planner_snapshot_is_not_merged_with_old_acceptance_or_deleted_tasks():
    before = {"repro_tasks": [{"task_id": "a", "scientific_acceptance": {"old": 10}}, {"task_id": "b"}]}
    after = {"repro_tasks": [{"task_id": "a", "scientific_acceptance": {"current": 1e-10}}], "extra": "owner explanation"}
    old = deepcopy(before)
    assert reconcile_final_tasks(before, after, {}) == after
    assert before == old


def test_unrecognized_field_status_is_preserved_for_contextual_review():
    requests = [{"request_id": "r", "required_fields": [{"field_id": "n"}]}]
    field = {"field_id": "n", "status": "found in caption", "note": "n=7", "fact_refs": []}
    summary = summarize_backfill_resolution(requests, {"engineering_facts": []},
        {"request_resolutions": [{"request_id": "r", "field_results": [field]}]})
    assert summary["open"][0]["field_results"][0] == field
    assert summary["classification"].startswith("owner_reported_status_only")


def test_followup_partial_answer_does_not_overwrite_previously_reported_evidence():
    facts = {"engineering_facts": []}
    first_request = [{"request_id": "setup", "required_fields": [{"field_id": "normalization"}]}]
    first = summarize_backfill_resolution(first_request, facts, {"request_resolutions": [{
        "request_id": "setup", "field_results": [{"field_id": "normalization", "status": "resolved_explicit", "note": "unit power"}]}]})
    ledger = update_search_ledger({}, round_index=1, requests=first_request, resolution=first)
    revised = [{"request_id": "setup", "required_fields": [{"field_id": "normalization"}, {"field_id": "trial_count"}]}]
    second = summarize_backfill_resolution(revised, facts, {"request_resolutions": [{
        "request_id": "setup", "field_results": [{"field_id": "trial_count", "status": "not_found_in_paper", "note": "not disclosed"}]}]})
    ledger = update_search_ledger(ledger, round_index=2, requests=revised, resolution=second)
    assert len(ledger["entries"]) == 2
    latest = {item["field_id"]: item for item in ledger["latest"]}
    assert latest["normalization"]["status"] == "resolved_explicit"
    assert latest["normalization"]["round"] == 1
    assert latest["trial_count"]["status"] == "not_found_in_paper"
    assert cumulative_resolution_from_ledger(revised, facts, ledger)["terminal_unresolved_count"] == 1
