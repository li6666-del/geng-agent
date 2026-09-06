"""Static capability evidence analysis for delivered Foundation tests."""

from __future__ import annotations

import ast
from typing import Any

from .foundation_architecture import library_keys as _library_keys
from .foundation_bindings import _ast_dotted_name
from .foundation_execution_policy import (
    CAPABILITY_GROUPS as _CAPABILITY_GROUPS,
    FRAMEWORK_SEMANTIC_LABELS as _FRAMEWORK_SEMANTIC_LABELS,
    TRUSTED_CAPABILITY_PROBE_FRAMEWORKS as _TRUSTED_CAPABILITY_PROBE_FRAMEWORKS,
)
from .foundation_test_catalog import (
    _DeliveredTest,
    _capability_matches_group,
    _capability_status_passed,
    _component_test_target,
    _flow_reference,
    _normalized_capability,
    _normalized_test_reference,
    _resolve_test_name,
    _test_import_targets,
)


def _contract_values_equal(expected: Any, actual: Any) -> bool:
    if isinstance(expected, bool) or isinstance(actual, bool):
        return type(expected) is type(actual) and expected == actual
    if isinstance(expected, list) and isinstance(actual, list):
        expected_items = sorted({str(item).strip().casefold() for item in expected})
        actual_items = sorted({str(item).strip().casefold() for item in actual})
        return expected_items == actual_items
    if isinstance(expected, str) and isinstance(actual, str):
        return expected.strip().casefold() == actual.strip().casefold()
    return expected == actual


def _framework_has_trusted_capability_probe(value: Any) -> bool:
    keys = _library_keys(value)
    return any(
        keys & _library_keys(framework)
        for framework in _TRUSTED_CAPABILITY_PROBE_FRAMEWORKS
    )


def _execution_requires_trusted_capability_probe(execution: dict[str, Any]) -> bool:
    if execution.get("trainable") is True:
        return True
    if str(execution.get("gradient_mode") or "").strip().casefold() == "required":
        return True
    if str(execution.get("checkpoint_policy") or "").strip().casefold() == "required":
        return True
    if str(execution.get("device_policy") or "").strip().casefold() == "accelerator_required":
        return True
    raw_capabilities = execution.get("required_capabilities")
    if not isinstance(raw_capabilities, list):
        return False
    probe_groups = (
        _CAPABILITY_GROUPS["parameter update"],
        _CAPABILITY_GROUPS["gradient/back-propagation"],
        _CAPABILITY_GROUPS["checkpoint round-trip"],
        _CAPABILITY_GROUPS["accelerator availability"],
        _CAPABILITY_GROUPS["accelerator tensor placement"],
    )
    return any(
        any(
            _capability_matches_group(capability, aliases)
            for aliases in probe_groups
        )
        for capability in raw_capabilities
    )


def _factory_expression_tags(
    node: ast.AST | None,
    *,
    local_values: dict[str, set[str]],
    import_targets: dict[str, str],
    component_target: str,
    component_is_class: bool,
    known_factories: dict[str, set[str]],
) -> set[str]:
    if isinstance(node, ast.Name):
        return set(local_values.get(node.id, set()))
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return set().union(
            *(
                _factory_expression_tags(
                    item,
                    local_values=local_values,
                    import_targets=import_targets,
                    component_target=component_target,
                    component_is_class=component_is_class,
                    known_factories=known_factories,
                )
                for item in node.elts
            ),
            set(),
        )
    if isinstance(node, ast.IfExp):
        return _factory_expression_tags(
            node.body,
            local_values=local_values,
            import_targets=import_targets,
            component_target=component_target,
            component_is_class=component_is_class,
            known_factories=known_factories,
        ) | _factory_expression_tags(
            node.orelse,
            local_values=local_values,
            import_targets=import_targets,
            component_target=component_target,
            component_is_class=component_is_class,
            known_factories=known_factories,
        )
    if not isinstance(node, ast.Call):
        return set()
    call_name = _ast_dotted_name(node.func)
    resolved = _resolve_test_name(call_name, import_targets)
    if resolved == component_target:
        return {"instance"} if component_is_class else {"instance", "output"}
    factory = call_name.split(".")[-1]
    return set(known_factories.get(factory, set()))


def _component_factory_tags(
    tree: ast.Module,
    test_class: ast.ClassDef,
    *,
    import_targets: dict[str, str],
    component_target: str,
    component_is_class: bool,
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    functions = [
        node
        for node in [*tree.body, *test_class.body]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and not node.name.startswith("test")
    ]
    factories: dict[str, set[str]] = {}
    fixture_parameters: dict[str, set[str]] = {}
    for _ in range(len(functions) + 1):
        changed = False
        for function in functions:
            local_values: dict[str, set[str]] = {}
            returned: set[str] = set()
            for statement in function.body:
                if isinstance(statement, (ast.Assign, ast.AnnAssign)):
                    value = statement.value
                    tags = _factory_expression_tags(
                        value,
                        local_values=local_values,
                        import_targets=import_targets,
                        component_target=component_target,
                        component_is_class=component_is_class,
                        known_factories=factories,
                    )
                    targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                    for target in targets:
                        if isinstance(target, ast.Name) and tags:
                            local_values[target.id] = set(tags)
                elif isinstance(statement, ast.Return):
                    returned |= _factory_expression_tags(
                        statement.value,
                        local_values=local_values,
                        import_targets=import_targets,
                        component_target=component_target,
                        component_is_class=component_is_class,
                        known_factories=factories,
                    )
            if returned and returned != factories.get(function.name):
                factories[function.name] = returned
                changed = True
        if not changed:
            break
    for function in functions:
        if any(
            "fixture" in _ast_dotted_name(decorator).casefold()
            for decorator in function.decorator_list
        ):
            tags = factories.get(function.name)
            if tags:
                fixture_parameters[function.name] = set(tags)
    return factories, fixture_parameters


def _new_component_flow(
    *,
    tree: ast.Module,
    test_class: ast.ClassDef,
    component: dict[str, Any],
) -> dict[str, Any]:
    import_targets = _test_import_targets(tree)
    component_target, component_is_class = _component_test_target(component)
    factories, fixture_parameters = _component_factory_tags(
        tree,
        test_class,
        import_targets=import_targets,
        component_target=component_target,
        component_is_class=component_is_class,
    )
    callable_name = str(component.get("callable") or "").strip()
    return {
        "values": {},
        "import_targets": import_targets,
        "component_target": component_target,
        "component_is_class": component_is_class,
        "component_method": callable_name.split(".")[-1] if "." in callable_name else "",
        "factories": factories,
        "fixture_parameters": fixture_parameters,
        "actions": set(),
        "assertion_tags": set(),
        "change_assertion": False,
        "component_interactions": 0,
        "checkpoint_saved": False,
    }


def _assign_flow_target(target: ast.AST, tags: set[str], flow: dict[str, Any]) -> None:
    if isinstance(target, (ast.Tuple, ast.List)):
        for item in target.elts:
            _assign_flow_target(item, tags, flow)
        return
    reference = _flow_reference(target)
    if reference and tags:
        flow["values"][reference] = set(tags)


def _flow_expression_tags(
    node: ast.AST | None,
    flow: dict[str, Any],
    *,
    record_actions: bool,
) -> set[str]:
    if node is None:
        return set()
    reference = _flow_reference(node)
    if reference in flow["values"]:
        return set(flow["values"][reference])
    if isinstance(node, ast.Name):
        return set()
    if isinstance(node, ast.Subscript):
        return _flow_expression_tags(node.value, flow, record_actions=record_actions)
    if isinstance(node, ast.Attribute):
        base_tags = _flow_expression_tags(node.value, flow, record_actions=record_actions)
        if node.attr in {"__class__", "__name__", "__qualname__", "__module__"}:
            return set()
        if "instance" in base_tags:
            flow["component_interactions"] += 1
            if node.attr.casefold() in {"grad", "gradient", "gradients"}:
                return {"gradient"}
            if node.attr.casefold() in {"device", "is_cuda"}:
                return {"device"}
            return {"parameter"}
        if "parameter" in base_tags and node.attr.casefold() in {"grad", "gradient", "gradients"}:
            return {"gradient"}
        if base_tags & {"instance", "parameter", "output"} and node.attr.casefold() in {"device", "is_cuda"}:
            return {"device"}
        return set(base_tags)
    if isinstance(node, ast.Call):
        call_name = _ast_dotted_name(node.func)
        resolved = _resolve_test_name(call_name, flow["import_targets"])
        if resolved == flow["component_target"]:
            if flow["component_is_class"]:
                return {"instance"}
            flow["component_interactions"] += 1
            return {"instance", "output"}
        factory = call_name.split(".")[-1]
        if factory in flow["factories"]:
            return set(flow["factories"][factory])

        callable_reference = _flow_reference(node.func)
        callable_tags = set(flow["values"].get(callable_reference, set()))
        if "instance" in callable_tags:
            flow["component_interactions"] += 1
            return {"output"}
        receiver_tags = (
            _flow_expression_tags(node.func.value, flow, record_actions=record_actions)
            if isinstance(node.func, ast.Attribute)
            else _flow_expression_tags(node.func, flow, record_actions=record_actions)
        )
        argument_tags = set().union(
            *(
                _flow_expression_tags(argument, flow, record_actions=record_actions)
                for argument in [*node.args, *(keyword.value for keyword in node.keywords)]
            ),
            set(),
        )
        method = _normalized_capability(call_name.split(".")[-1])
        resolved_tokens = {
            _normalized_capability(part)
            for part in resolved.split(".")
            if _normalized_capability(part)
        }

        if "instance" in receiver_tags:
            flow["component_interactions"] += 1
            if method in {"parameter", "parameters", "named-parameters"}:
                return {"parameter"}
            if method in {"state-dict", "get-state", "pack", "serialize"}:
                if record_actions:
                    flow["actions"].add("checkpoint_save")
                    flow["checkpoint_saved"] = True
                return {"checkpoint"}
            if method in {"load-state-dict", "set-state", "unpack", "restore", "deserialize"}:
                if record_actions and "checkpoint" in argument_tags:
                    flow["actions"].add("checkpoint_load")
                return {"instance"}
            if method in {"to", "cuda", "xpu", "mps", "put", "place"}:
                if record_actions:
                    flow["actions"].add("accelerator_placement")
                return {"instance", "device"}
            if method in {"apply", "fit", "minimise", "minimize", "step", "train-step", "update"}:
                if record_actions:
                    flow["actions"].add("parameter_update")
                return {"output"}
            return {"output"}

        if "optimizer" in receiver_tags:
            if record_actions and method in {"apply", "step", "train-step", "update"}:
                flow["actions"].add("parameter_update")
            return set()
        if receiver_tags & {"output", "parameter"}:
            if record_actions and method in {"backward", "grad", "gradient", "vjp"}:
                flow["actions"].add("gradient")
                return {"gradient"}
            return set(receiver_tags)

        if "parameter" in argument_tags and (
            "optim" in resolved_tokens
            or method in {"adagrad", "adam", "adamw", "optimizer", "rmsprop", "sgd"}
        ):
            return {"optimizer"}
        if record_actions and method in {"backward", "grad", "gradient", "vjp"} and argument_tags & {
            "output",
            "parameter",
        }:
            flow["actions"].add("gradient")
            return {"gradient"}
        if record_actions and method in {"available", "availability", "is-available"}:
            flow["actions"].add("accelerator_availability")
        trusted_namespace = resolved.split(".", 1)[0].casefold() in {
            "torch",
            "pickle",
            "joblib",
        }
        standalone_helper = isinstance(node.func, ast.Name)
        if method in {"dump", "save", "save-file", "serialize", "write"}:
            if argument_tags & {"checkpoint", "parameter", "instance"} and (
                trusted_namespace or standalone_helper
            ):
                if record_actions:
                    flow["actions"].add("checkpoint_save")
                    flow["checkpoint_saved"] = True
                return {"checkpoint"}
        if method in {"deserialize", "load", "load-file", "read", "restore"}:
            if flow["checkpoint_saved"] and (trusted_namespace or standalone_helper):
                if record_actions:
                    flow["actions"].add("checkpoint_load")
                return {"checkpoint"}
        if argument_tags:
            return set(argument_tags)
        return set().union(
            *(
                _flow_expression_tags(child, flow, record_actions=record_actions)
                for child in ast.iter_child_nodes(node)
                if child is not node.func
            ),
            set(),
        )
    if isinstance(node, ast.NamedExpr):
        tags = _flow_expression_tags(node.value, flow, record_actions=record_actions)
        _assign_flow_target(node.target, tags, flow)
        return tags
    return set().union(
        *(
            _flow_expression_tags(child, flow, record_actions=record_actions)
            for child in ast.iter_child_nodes(node)
        ),
        set(),
    )


def _assertion_operand_tags(call: ast.Call, flow: dict[str, Any]) -> set[str]:
    name = _ast_dotted_name(call.func).split(".")[-1].casefold()
    if not (name.startswith("assert") or name.startswith("failunless")):
        return set()
    operands = call.args[:1] if name in {
        "assertfalse",
        "assertisnone",
        "assertisnotnone",
        "asserttrue",
        "failunless",
    } else call.args[:2]
    return set().union(
        *(
            _flow_expression_tags(operand, flow, record_actions=True)
            for operand in operands
        ),
        set(),
    )


def _component_change_pair(
    left: ast.AST,
    right: ast.AST,
    flow: dict[str, Any],
) -> bool:
    if ast.dump(left, include_attributes=False) == ast.dump(right, include_attributes=False):
        return False
    material = {"checkpoint", "parameter"}
    left_tags = _flow_expression_tags(left, flow, record_actions=True)
    right_tags = _flow_expression_tags(right, flow, record_actions=True)
    return bool(left_tags & material and right_tags & material)


def _negated_equality_has_component_change(node: ast.AST, flow: dict[str, Any]) -> bool:
    if isinstance(node, ast.Call):
        name = _normalized_capability(_ast_dotted_name(node.func).split(".")[-1])
        if name in {"allclose", "equal", "isclose"} and len(node.args) >= 2:
            return _component_change_pair(node.args[0], node.args[1], flow)
    if isinstance(node, ast.Compare) and len(node.ops) == 1 and len(node.comparators) == 1:
        if isinstance(node.ops[0], ast.Eq):
            return _component_change_pair(node.left, node.comparators[0], flow)
    return False


def _assert_expression_has_component_change(node: ast.AST, flow: dict[str, Any]) -> bool:
    if isinstance(node, ast.Compare) and len(node.ops) == 1 and len(node.comparators) == 1:
        if isinstance(node.ops[0], ast.NotEq):
            return _component_change_pair(node.left, node.comparators[0], flow)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return _negated_equality_has_component_change(node.operand, flow)
    return False


def _assertion_call_has_component_change(call: ast.Call, flow: dict[str, Any]) -> bool:
    name = _normalized_capability(_ast_dotted_name(call.func).split(".")[-1])
    if name in {"assertnotalmostequal", "assertnotequal", "failifequal"}:
        return len(call.args) >= 2 and _component_change_pair(call.args[0], call.args[1], flow)
    if name == "assertfalse" and call.args:
        return _negated_equality_has_component_change(call.args[0], flow)
    if name == "asserttrue" and call.args:
        return _assert_expression_has_component_change(call.args[0], flow)
    return False


def _analyze_flow_statements(
    statements: list[ast.stmt],
    flow: dict[str, Any],
    *,
    record_actions: bool,
) -> None:
    for statement in statements:
        if isinstance(statement, (ast.Assign, ast.AnnAssign)):
            tags = _flow_expression_tags(statement.value, flow, record_actions=record_actions)
            targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            for target in targets:
                _assign_flow_target(target, tags, flow)
        elif isinstance(statement, ast.AugAssign):
            target_tags = _flow_expression_tags(statement.target, flow, record_actions=record_actions)
            value_tags = _flow_expression_tags(statement.value, flow, record_actions=record_actions)
            if record_actions and "parameter" in target_tags:
                flow["actions"].add("parameter_update")
            _assign_flow_target(statement.target, target_tags | value_tags, flow)
        elif isinstance(statement, ast.For):
            iter_tags = _flow_expression_tags(statement.iter, flow, record_actions=record_actions)
            _assign_flow_target(statement.target, iter_tags, flow)
            _analyze_flow_statements(statement.body, flow, record_actions=record_actions)
            _analyze_flow_statements(statement.orelse, flow, record_actions=record_actions)
        elif isinstance(statement, ast.Assert):
            tags = _flow_expression_tags(
                statement.test,
                flow,
                record_actions=record_actions,
            )
            if record_actions:
                flow["assertion_tags"] |= tags
                if _assert_expression_has_component_change(statement.test, flow):
                    flow["change_assertion"] = True
        elif isinstance(statement, ast.Expr):
            if record_actions and isinstance(statement.value, ast.Call):
                flow["assertion_tags"] |= _assertion_operand_tags(statement.value, flow)
                if _assertion_call_has_component_change(statement.value, flow):
                    flow["change_assertion"] = True
            _flow_expression_tags(statement.value, flow, record_actions=record_actions)
        elif isinstance(statement, (ast.If, ast.While)):
            _flow_expression_tags(statement.test, flow, record_actions=record_actions)
            _analyze_flow_statements(statement.body, flow, record_actions=record_actions)
            _analyze_flow_statements(statement.orelse, flow, record_actions=record_actions)
        elif isinstance(statement, (ast.With, ast.AsyncWith)):
            for item in statement.items:
                tags = _flow_expression_tags(item.context_expr, flow, record_actions=record_actions)
                if item.optional_vars is not None:
                    _assign_flow_target(item.optional_vars, tags, flow)
            _analyze_flow_statements(statement.body, flow, record_actions=record_actions)
        elif isinstance(statement, ast.Try):
            _analyze_flow_statements(statement.body, flow, record_actions=record_actions)
            for handler in statement.handlers:
                _analyze_flow_statements(handler.body, flow, record_actions=record_actions)
            _analyze_flow_statements(statement.orelse, flow, record_actions=record_actions)
            _analyze_flow_statements(statement.finalbody, flow, record_actions=record_actions)
        elif isinstance(statement, (ast.Return, ast.Raise)):
            _flow_expression_tags(
                statement.value if isinstance(statement, ast.Return) else statement.exc,
                flow,
                record_actions=record_actions,
            )


def _component_test_flow(
    delivered: _DeliveredTest,
    component: dict[str, Any],
) -> dict[str, Any]:
    method, tree, test_class = delivered
    flow = _new_component_flow(
        tree=tree,
        test_class=test_class,
        component=component,
    )
    module_assignments = [
        node for node in tree.body if isinstance(node, (ast.Assign, ast.AnnAssign))
    ]
    class_assignments = [
        node for node in test_class.body if isinstance(node, (ast.Assign, ast.AnnAssign))
    ]
    _analyze_flow_statements(module_assignments, flow, record_actions=False)
    _analyze_flow_statements(class_assignments, flow, record_actions=False)
    for node in test_class.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        is_setup = node.name in {
            "setUp",
            "setUpClass",
            "asyncSetUp",
            "asyncSetUpClass",
        }
        is_fixture = any(
            "fixture" in _ast_dotted_name(decorator).casefold()
            for decorator in node.decorator_list
        )
        if is_setup or is_fixture:
            _analyze_flow_statements(node.body, flow, record_actions=False)
    arguments = [
        *method.args.posonlyargs,
        *method.args.args,
        *method.args.kwonlyargs,
    ]
    for argument in arguments:
        fixture_tags = flow["fixture_parameters"].get(argument.arg)
        if fixture_tags:
            flow["values"][argument.arg] = set(fixture_tags)
    _analyze_flow_statements(method.body, flow, record_actions=True)
    return flow


def _method_references_component(flow: dict[str, Any]) -> bool:
    material_tags = {"checkpoint", "device", "gradient", "output", "parameter"}
    return bool(
        flow["component_interactions"]
        and flow["assertion_tags"] & material_tags
    )


def _method_evidences_capability(flow: dict[str, Any], label: str) -> bool:
    assertion_tags = flow["assertion_tags"]
    actions = flow["actions"]
    if label == "real parameter update":
        return "parameter_update" in actions and flow["change_assertion"] is True
    if label == "gradient/back-propagation":
        return "gradient" in actions and bool(assertion_tags & {"gradient", "parameter"})
    if label == "checkpoint round-trip":
        return {
            "checkpoint_save",
            "checkpoint_load",
        }.issubset(actions) and bool(assertion_tags & {"checkpoint", "parameter"})
    if label == "accelerator availability":
        return "accelerator_availability" in actions and bool(assertion_tags & {"device", "parameter"})
    if label == "accelerator tensor placement":
        return "accelerator_placement" in actions and "device" in assertion_tags
    return True


def _capability_test_passed(
    item: dict[str, Any],
    delivered_tests: dict[str, _DeliveredTest],
    *,
    component: dict[str, Any] | None = None,
    label: str | None = None,
) -> bool:
    reference = item.get("test") or item.get("test_id") or item.get("test_name")
    if not isinstance(reference, str) or not reference.strip():
        return False
    delivered = delivered_tests.get(_normalized_test_reference(reference))
    if not _capability_status_passed(item) or delivered is None:
        return False
    if component is None:
        return True
    if not _contract_values_equal(component.get("module"), item.get("module")):
        return False
    if not _contract_values_equal(component.get("callable"), item.get("callable")):
        return False
    flow = _component_test_flow(delivered, component)
    if not _method_references_component(flow):
        return False
    if label is None:
        return True
    if label not in _FRAMEWORK_SEMANTIC_LABELS:
        return True
    execution = component.get("execution")
    framework = execution.get("primary_framework") if isinstance(execution, dict) else ""
    if not _framework_has_trusted_capability_probe(framework):
        return False
    return _method_evidences_capability(flow, label)


def _required_component_capabilities(
    component: dict[str, Any],
) -> list[tuple[str, set[str], bool]]:
    execution = component.get("execution")
    if not isinstance(execution, dict):
        return []
    requirements: list[tuple[str, set[str], bool]] = []
    raw_capabilities = execution.get("required_capabilities")
    if isinstance(raw_capabilities, list):
        for capability in raw_capabilities:
            normalized = _normalized_capability(capability)
            if normalized:
                requirements.append(
                    (f"required capability {capability}", {normalized}, False)
                )
    if execution.get("trainable") is True:
        requirements.append(
            ("real parameter update", _CAPABILITY_GROUPS["parameter update"], True)
        )
    if str(execution.get("gradient_mode") or "").strip().casefold() == "required":
        requirements.append(
            (
                "gradient/back-propagation",
                _CAPABILITY_GROUPS["gradient/back-propagation"],
                True,
            )
        )
    if str(execution.get("checkpoint_policy") or "").strip().casefold() == "required":
        requirements.append(
            (
                "checkpoint round-trip",
                _CAPABILITY_GROUPS["checkpoint round-trip"],
                True,
            )
        )
    if str(execution.get("device_policy") or "").strip().casefold() == "accelerator_required":
        requirements.append(
            (
                "accelerator availability",
                _CAPABILITY_GROUPS["accelerator availability"],
                True,
            )
        )
        requirements.append(
            (
                "accelerator tensor placement",
                _CAPABILITY_GROUPS["accelerator tensor placement"],
                True,
            )
        )
    if str(execution.get("device_policy") or "").strip().casefold() == "external_runtime":
        requirements.append(
            (
                "external runtime availability",
                _CAPABILITY_GROUPS["external runtime availability"],
                True,
            )
        )
        requirements.append(
            (
                "external runtime invocation interface",
                _CAPABILITY_GROUPS["external runtime invocation interface"],
                True,
            )
        )
    return requirements
