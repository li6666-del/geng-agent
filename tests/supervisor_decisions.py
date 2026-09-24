"""Deterministic *test-only* scheduling for owner/adapter unit tests.

The real controller and model transport are exercised in test_supervisor_tools.
This policy is not imported by the application and does not test LLM judgment.
"""
def choose_tools(context):
    ready = list(context.get("ready", []))
    state = context.get("state", {})
    active = context.get("active", [])
    history = context.get("history", [])
    failed = {item["tool"] for item in history if item.get("status") == "failed"}
    if "failures" in state:
        failed = set(state["failures"])
    chosen = []
    if state.get("delivery_available"):
        return {"action": "finish", "diagnosis": "离线测试交付完成"}
    if "search" in ready or "replan" in ready:
        tool = (context.get("tools") or [{}])[0]
        inputs = tool.get("inputs", {})
        handoff = inputs.get("planner_handoff", {})
        if handoff.get("ready_for_writer") is False and "search" in ready and "search" not in failed:
            chosen = ["search"]
    elif "reports" in ready and state.get("failures"):
        chosen = ["reports"] if "reports" not in failed else []
    elif "deliver_partial" in ready and "environment" in failed:
        chosen = ["deliver_partial"]
    else:
        resources = set()
        order = {name: index for index, name in enumerate(("environment", "foundation", "writers", "retain_foundation", "deliver_partial"))}
        for tool in sorted(context.get("tools", []), key=lambda tool: order.get(tool["name"], 10)):
            name = tool["name"]
            if name not in ready or name in failed or name in {"revise_plan", "revise_understanding"}:
                continue
            owned = set(tool.get("resources", []))
            if resources.intersection(owned):
                continue
            chosen.append(name)
            resources.update(owned)
    return {"action": "start" if chosen else "wait" if active else "finish", "next_nodes": chosen,
            "next_node": chosen[0] if chosen else "", "diagnosis": "离线测试指定调度",
            "instructions": "按测试安排执行，保留失败", "expected_change": "可观察的工具结果", "decision_id": "offline"}
