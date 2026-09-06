"""Deterministic compilation of logical reproduction tasks into execution units.

The task document remains the scientific contract.  This module only compiles
explicit execution relationships:

* ``strong`` relationships co-locate their tasks in one Writer sandbox;
* ``weak`` relationships preserve cross-task consistency information without
  co-locating tasks;
* directed artifact dependencies retain producer -> consumer provenance.

No relationship is inferred from task wording, figure numbers, or artifact
filenames.  That keeps the compiler deterministic and paper-agnostic.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
import re
from typing import Any


__all__ = [
    "ExecutionPlanError",
    "compile_execution_plan",
    "validate_execution_relationships",
    "validate_relationships",
]


class ExecutionPlanError(ValueError):
    """An execution relationship cannot be compiled responsibly."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "invalid_execution_plan",
        path: str = "$",
    ) -> None:
        self.code = code
        self.path = path
        self.message = message
        super().__init__(f"{path}: {message}")


class _UnionFind:
    def __init__(self, values: Sequence[str]) -> None:
        self._parent = {value: value for value in values}
        self._rank = {value: 0 for value in values}

    def find(self, value: str) -> str:
        parent = self._parent[value]
        if parent != value:
            self._parent[value] = self.find(parent)
        return self._parent[value]

    def union(self, left: str, right: str) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root == right_root:
            return
        left_rank = self._rank[left_root]
        right_rank = self._rank[right_root]
        if left_rank < right_rank:
            left_root, right_root = right_root, left_root
        self._parent[right_root] = left_root
        if left_rank == right_rank:
            self._rank[left_root] += 1


def validate_execution_relationships(
    repro_tasks_document: Any,
    relationships: Any | None = None,
) -> None:
    """Validate task identities and explicit strong/weak relationships.

    ``repro_tasks_document`` may be a plain mapping or an object exposing
    ``model_dump()`` (for example a Pydantic model).  Relationships normally
    live under ``execution_relationships``; ``relationships`` is accepted as a
    document alias or as this function's explicit second argument.

    The function returns ``None`` and raises :class:`ExecutionPlanError` for
    substantive ambiguity such as unknown tasks, an invalid directed edge, a
    strong dependency cycle, or multiple producers for one artifact.
    """

    _validated_input(repro_tasks_document, relationships)


def validate_relationships(
    repro_tasks_document: Any,
    relationships: Any | None = None,
) -> None:
    """Short alias for :func:`validate_execution_relationships`."""

    validate_execution_relationships(repro_tasks_document, relationships)


def compile_execution_plan(
    repro_tasks_document: Any,
    relationships: Any | None = None,
) -> dict[str, Any]:
    """Compile a JSON-serializable deterministic execution plan.

    Strong connected components become execution units.  Tasks not mentioned
    by any strong relationship remain singleton units.  Weak relationships are
    represented separately and never influence unit membership.
    """

    task_ids, normalized = _validated_input(repro_tasks_document, relationships)
    task_order = {task_id: index for index, task_id in enumerate(task_ids)}

    strong = _UnionFind(task_ids)
    for relationship in normalized:
        if relationship["strength"] != "strong":
            continue
        members = relationship["task_ids"]
        for task_id in members[1:]:
            strong.union(members[0], task_id)

    components: dict[str, list[str]] = {}
    for task_id in task_ids:
        components.setdefault(strong.find(task_id), []).append(task_id)
    ordered_components = sorted(
        components.values(),
        key=lambda members: (min(task_order[item] for item in members), tuple(members)),
    )
    ordered_components = [
        _topologically_order_strong_component(
            members,
            relationships=normalized,
            task_order=task_order,
        )
        for members in ordered_components
    ]

    task_to_unit: dict[str, str] = {}
    unit_members: list[tuple[str, list[str]]] = []
    for members in ordered_components:
        unit_id = _stable_scoped_id("unit", members)
        unit_members.append((unit_id, members))
        for task_id in members:
            task_to_unit[task_id] = unit_id

    normalized, artifact_scopes = _scope_reused_producer_artifacts(
        normalized,
        task_to_unit=task_to_unit,
    )
    dependencies = _artifact_dependencies(normalized, task_to_unit)
    execution_units: list[dict[str, Any]] = []
    for unit_id, members in unit_members:
        member_set = set(members)
        unit_relationships = [
            _json_copy(relationship)
            for relationship in normalized
            if member_set.intersection(relationship["task_ids"])
        ]
        unit_dependencies = [
            _json_copy(dependency)
            for dependency in dependencies
            if unit_id
            in {
                dependency["producer_execution_unit_id"],
                dependency["consumer_execution_unit_id"],
            }
        ]
        artifact_ids = sorted(
            {
                artifact_id
                for relationship in unit_relationships
                for artifact_id in relationship["artifact_ids"]
            }
        )
        execution_units.append(
            {
                "unit_id": unit_id,
                "task_ids": list(members),
                "mode": "compound" if len(members) > 1 else "singleton",
                "relationships": unit_relationships,
                "dependencies": unit_dependencies,
                "artifact_ids": artifact_ids,
            }
        )

    weak_groups = _compile_weak_groups(
        task_ids=task_ids,
        relationships=normalized,
        task_to_unit=task_to_unit,
        ordered_unit_ids=[unit_id for unit_id, _ in unit_members],
    )
    plan = {
        "schema_version": "1.0",
        "logical_task_count": len(task_ids),
        "execution_unit_count": len(execution_units),
        "execution_units": execution_units,
        "task_to_execution_unit": {
            task_id: task_to_unit[task_id] for task_id in task_ids
        },
        "weak_consistency_groups": weak_groups,
        "artifact_scopes": artifact_scopes,
    }
    # Fail locally if a future edit accidentally leaks a set, model, or Path.
    json.dumps(plan, ensure_ascii=False, sort_keys=True)
    return plan


def _validated_input(
    repro_tasks_document: Any,
    explicit_relationships: Any | None,
) -> tuple[list[str], list[dict[str, Any]]]:
    document = _as_mapping(repro_tasks_document, path="$")
    raw_tasks = document.get("repro_tasks", document.get("tasks"))
    if not _is_sequence(raw_tasks):
        raise ExecutionPlanError(
            "repro_tasks must be a list",
            code="invalid_tasks",
            path="$.repro_tasks",
        )

    task_ids: list[str] = []
    seen_tasks: set[str] = set()
    for index, raw_task in enumerate(raw_tasks):
        task = _as_mapping(raw_task, path=f"$.repro_tasks[{index}]")
        task_id = _nonempty_text(task.get("task_id"))
        if not task_id:
            raise ExecutionPlanError(
                "task_id must be a non-empty string",
                code="invalid_task_id",
                path=f"$.repro_tasks[{index}].task_id",
            )
        if task_id in seen_tasks:
            raise ExecutionPlanError(
                f"duplicate task_id {task_id!r}",
                code="duplicate_task_id",
                path=f"$.repro_tasks[{index}].task_id",
            )
        seen_tasks.add(task_id)
        task_ids.append(task_id)

    raw_relationships = explicit_relationships
    relationship_path = "$.execution_relationships"
    if raw_relationships is None:
        if "execution_relationships" in document:
            raw_relationships = document.get("execution_relationships")
        elif "relationships" in document:
            relationship_path = "$.relationships"
            raw_relationships = document.get("relationships")
        else:
            raw_relationships = []
    else:
        relationship_path = "$relationships"
    if not _is_sequence(raw_relationships):
        raise ExecutionPlanError(
            "execution relationships must be a list",
            code="invalid_relationships",
            path=relationship_path,
        )

    normalized: list[dict[str, Any]] = []
    seen_relationship_ids: set[str] = set()
    for index, raw_relationship in enumerate(raw_relationships):
        path = f"{relationship_path}[{index}]"
        relationship = _normalize_relationship(raw_relationship, path=path)
        relationship_id = relationship["relationship_id"]
        if relationship_id in seen_relationship_ids:
            raise ExecutionPlanError(
                f"duplicate relationship_id {relationship_id!r}",
                code="duplicate_relationship_id",
                path=f"{path}.relationship_id",
            )
        seen_relationship_ids.add(relationship_id)
        normalized.append(relationship)

    normalized.sort(key=_relationship_sort_key)
    _validate_relationship_references(task_ids, normalized)
    _validate_artifact_producers(task_ids, normalized)
    _validate_strong_dependency_graph(task_ids, normalized)
    return task_ids, normalized


def _normalize_relationship(value: Any, *, path: str) -> dict[str, Any]:
    raw = _as_mapping(value, path=path)
    kind = _nonempty_text(raw.get("kind")).lower() or "other"
    if kind not in {
        "same_run_outputs",
        "checkpoint_flow",
        "shared_pretraining",
        "shared_random_realization",
        "shared_dataset_partition",
        "shared_definition",
        "other",
    }:
        kind = "other"
    rationale = str(raw.get("rationale") or "").strip()
    strength = _nonempty_text(raw.get("strength")).lower()
    if strength not in {"strong", "weak"}:
        raise ExecutionPlanError(
            "strength must be 'strong' or 'weak'",
            code="invalid_relationship_strength",
            path=f"{path}.strength",
        )

    raw_members = raw.get("task_ids", raw.get("tasks"))
    if not _is_sequence(raw_members):
        raise ExecutionPlanError(
            "task_ids must be a list containing at least two task IDs",
            code="invalid_relationship_tasks",
            path=f"{path}.task_ids",
        )
    members = _unique_nonempty_strings(raw_members, path=f"{path}.task_ids")
    if len(members) < 2:
        raise ExecutionPlanError(
            "a relationship must contain at least two distinct task IDs",
            code="invalid_relationship_tasks",
            path=f"{path}.task_ids",
        )

    producer = _optional_text(raw.get("producer_task_id", raw.get("producer")))
    if "consumer_task_ids" in raw:
        raw_consumers = raw.get("consumer_task_ids")
        if not _is_sequence(raw_consumers):
            raise ExecutionPlanError(
                "consumer_task_ids must be a list",
                code="invalid_dependency_consumers",
                path=f"{path}.consumer_task_ids",
            )
        consumers = _unique_nonempty_strings(
            raw_consumers, path=f"{path}.consumer_task_ids"
        )
    else:
        legacy_consumer = raw.get("consumer_task_id", raw.get("consumer"))
        if legacy_consumer is None:
            consumers = []
        elif _is_sequence(legacy_consumer):
            consumers = _unique_nonempty_strings(
                legacy_consumer, path=f"{path}.consumer_task_ids"
            )
        else:
            consumer = _optional_text(legacy_consumer)
            if consumer is None:
                raise ExecutionPlanError(
                    "consumer must be a non-empty task ID",
                    code="invalid_dependency_consumers",
                    path=f"{path}.consumer_task_ids",
                )
            consumers = [consumer]
    if (producer is None) != (not consumers):
        raise ExecutionPlanError(
            "producer_task_id and at least one consumer_task_ids entry must be supplied together",
            code="incomplete_dependency",
            path=path,
        )
    if kind in {"checkpoint_flow", "shared_pretraining"} and (
        strength != "strong" or producer is None
    ):
        raise ExecutionPlanError(
            "shared trained state requires a strong relationship with one producer and explicit consumers; shared source alone does not share checkpoints",
            code="missing_shared_state_producer",
            path=path,
        )
    if producer is not None and strength == "weak":
        raise ExecutionPlanError(
            "a producer/consumer artifact flow must be strong; weak relationships may share only frozen definitions",
            code="weak_runtime_artifact_flow",
            path=path,
        )

    raw_artifacts = raw.get("artifact_ids", raw.get("artifacts"))
    if raw_artifacts is None and "artifact_id" in raw:
        raw_artifacts = [raw.get("artifact_id")]
    if raw_artifacts is None and "artifact" in raw:
        raw_artifacts = [raw.get("artifact")]
    if raw_artifacts is None:
        artifact_ids: list[str] = []
    elif _is_sequence(raw_artifacts):
        artifact_ids = _unique_nonempty_strings(
            raw_artifacts, path=f"{path}.artifact_ids"
        )
    else:
        raise ExecutionPlanError(
            "artifact_ids must be a list",
            code="invalid_artifacts",
            path=f"{path}.artifact_ids",
        )
    if producer is not None and not artifact_ids:
        raise ExecutionPlanError(
            "a directed producer/consumer dependency requires at least one artifact_id",
            code="dependency_missing_artifact",
            path=f"{path}.artifact_ids",
        )

    relationship_id = _nonempty_text(
        raw.get("relationship_id", raw.get("id"))
    )
    if not relationship_id:
        identity = {
            "kind": kind,
            "strength": strength,
            "task_ids": sorted(members),
            "producer_task_id": producer,
            "consumer_task_ids": sorted(consumers),
            "artifact_ids": sorted(artifact_ids),
        }
        relationship_id = "relationship_" + hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:12]

    return {
        "relationship_id": relationship_id,
        "kind": kind,
        "strength": strength,
        "task_ids": members,
        "producer_task_id": producer,
        "consumer_task_ids": consumers,
        "artifact_ids": sorted(artifact_ids),
        "rationale": rationale,
    }


def _validate_relationship_references(
    task_ids: list[str], relationships: list[dict[str, Any]]
) -> None:
    known = set(task_ids)
    for relationship in relationships:
        relationship_id = relationship["relationship_id"]
        for task_id in relationship["task_ids"]:
            if task_id not in known:
                raise ExecutionPlanError(
                    f"relationship {relationship_id!r} references unknown task {task_id!r}",
                    code="unknown_task_reference",
                    path=f"$.execution_relationships[{relationship_id}].task_ids",
                )
        members = set(relationship["task_ids"])
        producer = relationship["producer_task_id"]
        endpoints = (
            [("producer_task_id", producer)] if producer is not None else []
        ) + [
            (f"consumer_task_ids[{index}]", task_id)
            for index, task_id in enumerate(relationship["consumer_task_ids"])
        ]
        for role, task_id in endpoints:
            if task_id not in known:
                raise ExecutionPlanError(
                    f"{role} references unknown task {task_id!r}",
                    code="unknown_task_reference",
                    path=f"$.execution_relationships[{relationship_id}].{role}",
                )
            if task_id not in members:
                raise ExecutionPlanError(
                    f"{role} {task_id!r} is not included in relationship.task_ids",
                    code="dependency_endpoint_outside_relationship",
                    path=f"$.execution_relationships[{relationship_id}].{role}",
                )


def _validate_artifact_producers(
    task_ids: list[str],
    relationships: list[dict[str, Any]],
) -> None:
    strong = _UnionFind(task_ids)
    for relationship in relationships:
        if relationship["strength"] != "strong":
            continue
        members = relationship["task_ids"]
        for task_id in members[1:]:
            strong.union(members[0], task_id)

    producer_by_artifact_scope: dict[tuple[str, str], str] = {}
    for relationship in relationships:
        producer = relationship["producer_task_id"]
        if producer is None:
            continue
        execution_scope = strong.find(producer)
        for artifact_id in relationship["artifact_ids"]:
            scope_key = (artifact_id, execution_scope)
            previous = producer_by_artifact_scope.get(scope_key)
            if previous is not None and previous != producer:
                raise ExecutionPlanError(
                    f"artifact {artifact_id!r} has multiple producers in one execution scope: "
                    f"{previous!r} and {producer!r}",
                    code="multiple_artifact_producers",
                    path=f"$.execution_relationships[{relationship['relationship_id']}].artifact_ids",
                )
            producer_by_artifact_scope[scope_key] = producer


def _scope_reused_producer_artifacts(
    relationships: list[dict[str, Any]],
    *,
    task_to_unit: dict[str, str],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Namespace generic artifact labels reused by independent execution units."""

    units_by_artifact: dict[str, set[str]] = {}
    producer_by_scope: dict[tuple[str, str], str] = {}
    for relationship in relationships:
        producer = relationship["producer_task_id"]
        if producer is None:
            continue
        unit_id = task_to_unit[producer]
        for artifact_id in relationship["artifact_ids"]:
            units_by_artifact.setdefault(artifact_id, set()).add(unit_id)
            producer_by_scope[(artifact_id, unit_id)] = producer

    reused = {
        artifact_id
        for artifact_id, unit_ids in units_by_artifact.items()
        if len(unit_ids) > 1
    }
    if not reused:
        return relationships, []

    scoped_relationships = _json_copy(relationships)
    scopes: dict[tuple[str, str], dict[str, str]] = {}
    for relationship in scoped_relationships:
        producer = relationship["producer_task_id"]
        if producer is None:
            continue
        unit_id = task_to_unit[producer]
        rewritten: list[str] = []
        for artifact_id in relationship["artifact_ids"]:
            if artifact_id not in reused:
                rewritten.append(artifact_id)
                continue
            scoped_id = _stable_scoped_artifact_id(artifact_id, unit_id)
            rewritten.append(scoped_id)
            scopes[(artifact_id, unit_id)] = {
                "source_artifact_id": artifact_id,
                "scoped_artifact_id": scoped_id,
                "producer_task_id": producer_by_scope[(artifact_id, unit_id)],
                "execution_unit_id": unit_id,
            }
        relationship["artifact_ids"] = sorted(dict.fromkeys(rewritten))
    return scoped_relationships, [scopes[key] for key in sorted(scopes)]


def _stable_scoped_artifact_id(artifact_id: str, unit_id: str) -> str:
    digest = hashlib.sha256(
        f"{artifact_id}\0{unit_id}".encode("utf-8")
    ).hexdigest()[:12]
    return f"{_slug(artifact_id)[:40]}__scope_{digest}"


def _validate_strong_dependency_graph(
    task_ids: list[str], relationships: list[dict[str, Any]]
) -> None:
    graph: dict[str, list[str]] = {task_id: [] for task_id in task_ids}
    for relationship in relationships:
        if relationship["strength"] != "strong":
            continue
        producer = relationship["producer_task_id"]
        if producer is None:
            continue
        for consumer in relationship["consumer_task_ids"]:
            if consumer not in graph[producer]:
                graph[producer].append(consumer)

    order = {task_id: index for index, task_id in enumerate(task_ids)}
    for neighbours in graph.values():
        neighbours.sort(key=order.__getitem__)
    state: dict[str, int] = {task_id: 0 for task_id in task_ids}
    stack: list[str] = []

    def visit(task_id: str) -> None:
        state[task_id] = 1
        stack.append(task_id)
        for consumer in graph[task_id]:
            if state[consumer] == 0:
                visit(consumer)
                continue
            if state[consumer] == 1:
                start = stack.index(consumer)
                cycle = stack[start:] + [consumer]
                raise ExecutionPlanError(
                    "strong dependency cycle: " + " -> ".join(cycle),
                    code="strong_dependency_cycle",
                    path="$.execution_relationships",
                )
        stack.pop()
        state[task_id] = 2

    for task_id in task_ids:
        if state[task_id] == 0:
            visit(task_id)


def _topologically_order_strong_component(
    members: list[str],
    *,
    relationships: list[dict[str, Any]],
    task_order: dict[str, int],
) -> list[str]:
    """Order one strong component by dependencies, then original task order."""

    member_set = set(members)
    adjacency: dict[str, set[str]] = {task_id: set() for task_id in members}
    indegree: dict[str, int] = {task_id: 0 for task_id in members}
    for relationship in relationships:
        if relationship["strength"] != "strong":
            continue
        producer = relationship["producer_task_id"]
        if producer is None or producer not in member_set:
            continue
        for consumer in relationship["consumer_task_ids"]:
            if consumer not in member_set or consumer in adjacency[producer]:
                continue
            adjacency[producer].add(consumer)
            indegree[consumer] += 1

    ready = sorted(
        (task_id for task_id in members if indegree[task_id] == 0),
        key=task_order.__getitem__,
    )
    ordered: list[str] = []
    while ready:
        task_id = ready.pop(0)
        ordered.append(task_id)
        for consumer in sorted(adjacency[task_id], key=task_order.__getitem__):
            indegree[consumer] -= 1
            if indegree[consumer] == 0:
                ready.append(consumer)
                ready.sort(key=task_order.__getitem__)

    if len(ordered) != len(members):
        # The full graph is validated before compilation; keep this defensive
        # check next to the ordering algorithm so it can never emit a partial unit.
        raise ExecutionPlanError(
            "strong dependency cycle prevented execution-unit ordering",
            code="strong_dependency_cycle",
            path="$.execution_relationships",
        )
    return ordered


def _artifact_dependencies(
    relationships: list[dict[str, Any]], task_to_unit: dict[str, str]
) -> list[dict[str, Any]]:
    dependencies: list[dict[str, Any]] = []
    for relationship in relationships:
        producer = relationship["producer_task_id"]
        consumers = relationship["consumer_task_ids"]
        if producer is None or not consumers:
            continue
        for consumer in consumers:
            for artifact_id in relationship["artifact_ids"]:
                dependencies.append(
                    {
                        "relationship_id": relationship["relationship_id"],
                        "strength": relationship["strength"],
                        "artifact_id": artifact_id,
                        "producer_task_id": producer,
                        "consumer_task_id": consumer,
                        "producer_execution_unit_id": task_to_unit[producer],
                        "consumer_execution_unit_id": task_to_unit[consumer],
                    }
                )
    dependencies.sort(
        key=lambda item: (
            item["artifact_id"],
            item["producer_task_id"],
            item["consumer_task_id"],
            item["relationship_id"],
        )
    )
    return dependencies


def _compile_weak_groups(
    *,
    task_ids: list[str],
    relationships: list[dict[str, Any]],
    task_to_unit: dict[str, str],
    ordered_unit_ids: list[str],
) -> list[dict[str, Any]]:
    del task_ids
    weak_relationships = [
        relationship
        for relationship in relationships
        if relationship["strength"] == "weak"
    ]
    if not weak_relationships:
        return []
    unit_order = {unit_id: index for index, unit_id in enumerate(ordered_unit_ids)}

    groups: list[dict[str, Any]] = []
    # Each weak relationship is its own (possibly overlapping) consistency
    # group. A transitive closure would incorrectly imply that A and C share
    # every definition merely because A<->B and B<->C are both declared.
    for relationship in weak_relationships:
        members = list(relationship["task_ids"])
        execution_unit_ids = sorted(
            {task_to_unit[task_id] for task_id in members},
            key=unit_order.__getitem__,
        )
        groups.append(
            {
                "group_id": _stable_scoped_id(
                    "weak_group",
                    [relationship["relationship_id"], *members],
                ),
                "task_ids": list(members),
                "execution_unit_ids": execution_unit_ids,
                "relationship_ids": [relationship["relationship_id"]],
                "relationships": [_json_copy(relationship)],
                "artifact_ids": sorted(relationship["artifact_ids"]),
            }
        )
    return groups


def _stable_scoped_id(prefix: str, task_ids: Sequence[str]) -> str:
    canonical = sorted(task_ids)
    digest = hashlib.sha256("\0".join(canonical).encode("utf-8")).hexdigest()[:10]
    label = _slug(canonical[0] if canonical else "empty")[:32]
    return f"{prefix}_{label}_{digest}"


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", value).strip("_").lower()
    return slug or "task"


def _relationship_sort_key(relationship: dict[str, Any]) -> tuple[Any, ...]:
    return (
        relationship["relationship_id"],
        relationship["kind"],
        relationship["strength"],
        tuple(sorted(relationship["task_ids"])),
        relationship["producer_task_id"] or "",
        tuple(relationship["consumer_task_ids"]),
        tuple(relationship["artifact_ids"]),
    )


def _as_mapping(value: Any, *, path: str) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(mode="python")
        if isinstance(dumped, Mapping):
            return dict(dumped)
    raise ExecutionPlanError(
        "must be a mapping or expose model_dump()",
        code="invalid_document",
        path=path,
    )


def _is_sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    )


def _unique_nonempty_strings(values: Sequence[Any], *, path: str) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for index, value in enumerate(values):
        text = _nonempty_text(value)
        if not text:
            raise ExecutionPlanError(
                "must contain only non-empty strings",
                code="invalid_identifier",
                path=f"{path}[{index}]",
            )
        if text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _nonempty_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = _nonempty_text(value)
    return text or None


def _json_copy(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True))
