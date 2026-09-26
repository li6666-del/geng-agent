"""Follow component addresses supplied by the planning agent."""
def task_component_ids(architecture):
    components = {str(c.get("id")): c for c in architecture.get("components", []) if isinstance(c, dict)}
    result = {}
    for binding in architecture.get("bindings", []):
        if not isinstance(binding, dict):
            continue
        selected = result.setdefault(str(binding.get("task_id") or ""), set())
        pending = list(binding.get("components") or [])
        while pending:
            key = str(pending.pop())
            if key in selected:
                continue
            selected.add(key)
            component = components.get(key, {})
            pending.extend(component.get("depends_on") or [])
    return {key: sorted(value) for key, value in result.items()}
