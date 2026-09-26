"""Planner-owned final task publication."""
from geng_agent.task_evidence_backfill import reconcile_final_tasks
from geng_agent.semantic_merge import semantic_merge_repro_tasks

def test_planner_can_replace_two_tasks_with_one_without_old_tasks_returning():
    old={"repro_tasks":[{"task_id":"a"},{"task_id":"b"}]}
    final={"repro_tasks":[{"task_id":"joint","experiments":[{"goal":"a"},{"goal":"b"}]}]}
    assert reconcile_final_tasks(old, final, {}) == final
    published,changed=semantic_merge_repro_tasks(old,final)
    assert published==final and changed==1
    assert len(old["repro_tasks"])==2
