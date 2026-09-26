from copy import deepcopy

import pytest

from geng_agent.architecture_protocol import ArchitectureAddressError, architecture_runtime_view


def _alias_architecture():
    # Synthetic values using the address shape emitted by the Planner. No case
    # evidence, scientific claims or paper-specific identifiers are copied here.
    return {
        "schema": "planner-document",
        "quantities": [{"quantity_id": "samples", "values": [0, -1], "basis": {"note": "显式假设"}}],
        "components": [
            {"component_id": "base", "module": "src/base.py", "depends_on": [],
             "contract": "保留原始条件", "parameters": [{"name": "scale", "value": 0}]},
            {"component_id": "shared", "module": "src/shared.py", "depends_on": ["base"]},
            {"component_id": "left", "module": "src/left.py", "depends_on": ["shared"]},
            {"component_id": "right", "module": "src/right.py", "depends_on": ["shared"]},
        ],
        "bindings": [
            {"task_id": "a", "experiment_id": "first", "component_ids": ["left"],
             "parameter_bindings": {"scale": 0}, "variant_note": "保留负值与零值",
             "output_quantity_ids": ["samples"], "primary_consistency_group_id": "comparison"},
            {"task_id": "b", "experiment_id": "second", "component_ids": ["right"]},
        ],
        "consistency_groups": [{"consistency_group_id": "comparison", "quantity_ids": ["samples"]}],
        "invariants": [{"invariant_id": "law", "statement": "原文陈述，不作真假判断", "quantity_ids": ["samples"]}],
        "unresolved_items": [{"note": "等待主持人判断"}],
    }


def test_addresses_are_added_without_mutating_or_rewriting_owner_document():
    original = _alias_architecture()
    before = deepcopy(original)
    view = architecture_runtime_view(original)

    assert original == before
    assert "schema_version" not in view
    for group, canonical in (("components", {"id"}), ("quantities", {"id"}),
                             ("bindings", {"components", "outputs", "consistency_group"}),
                             ("consistency_groups", {"id"}), ("invariants", {"id"})):
        for source, adapted in zip(original[group], view[group]):
            assert {key: value for key, value in adapted.items() if key not in canonical} == source
    assert view["quantities"][0]["id"] == "samples"
    assert view["consistency_groups"][0]["id"] == "comparison"
    assert view["invariants"][0]["id"] == "law"
    assert view["bindings"][0]["outputs"] == ["samples"]
    assert view["bindings"][0]["consistency_group"] == "comparison"
    assert "overrides" not in view["bindings"][0]
    assert "shared_quantity_ids" not in view["consistency_groups"][0]
    assert view["unresolved_items"] == original["unresolved_items"]
    view["components"][0]["parameters"][0]["value"] = 9
    view["bindings"][0]["components"].append("extra")
    assert original == before
    assert view["bindings"][0]["component_ids"] == ["left"]


@pytest.mark.parametrize("collection,canonical,alias,left,right", [
    ("components", "id", "component_id", "a", "b"),
    ("quantities", "id", "quantity_id", "a", "b"),
    ("bindings", "components", "component_ids", ["a", "b"], ["b", "a"]),
    ("bindings", "outputs", "output_quantity_ids", ["a"], ["b"]),
    ("bindings", "consistency_group", "primary_consistency_group_id", "a", "b"),
    ("consistency_groups", "id", "group_id", "a", "b"),
    ("consistency_groups", "id", "consistency_group_id", "a", "b"),
    ("invariants", "id", "invariant_id", "a", "b"),
    ("components", "id", "component_id", None, "a"),
    ("components", "id", "component_id", 1, True),
])
def test_conflicting_owner_addresses_are_preserved_without_host_rejection(collection, canonical, alias, left, right):
    source = {collection: [{"note": "preserved"}, {canonical: left, alias: right}]}
    before = deepcopy(source)
    view = architecture_runtime_view(source)
    expected = right if (collection, canonical, alias) == ("bindings", "outputs", "output_quantity_ids") else left
    assert view[collection][1][canonical] == expected
    assert view[collection][1][alias] == right
    assert source == before


def test_two_group_aliases_preserve_both_owner_addresses():
    conflicting = architecture_runtime_view({"consistency_groups": [{"group_id": "one", "consistency_group_id": "two"}]})
    assert conflicting["consistency_groups"][0] == {
        "id": "one", "group_id": "one", "consistency_group_id": "two"}
    view = architecture_runtime_view({"consistency_groups": [{"group_id": "same", "consistency_group_id": "same"}]})
    assert view["consistency_groups"][0] == {"id": "same", "group_id": "same", "consistency_group_id": "same"}


def test_artifact_outputs_are_not_confused_with_declared_quantity_outputs():
    source = {
        "quantities": [{"quantity_id": "q_result"}],
        "bindings": [{"task_id": "task", "outputs": ["ART_RESULT"],
                      "output_quantity_ids": ["q_result"]}],
    }
    view = architecture_runtime_view(source)
    assert view["bindings"][0]["outputs"] == ["q_result"]
    assert source["bindings"][0]["outputs"] == ["ART_RESULT"]
    assert architecture_runtime_view(view) == view
    other = architecture_runtime_view({"quantities": [{"id": "q_other"}],
                                       "bindings": source["bindings"]})
    assert other["bindings"][0]["outputs"] == ["q_result"]


def test_runtime_view_is_idempotent_and_accepts_equal_duplicate_addresses():
    first = architecture_runtime_view(_alias_architecture())
    second = architecture_runtime_view(first)
    assert second == first
    assert second is not first


def test_canonical_document_and_unknown_science_are_unchanged():
    source = {
        "components": [{"id": "shared", "contract": {"component_id": "scientific-text"}}],
        "bindings": [{"components": ["shared"], "task_id": "a", "outputs": ["q"], "consistency_group": "g"}],
        "quantities": [{"id": "q", "unknown_interpretation": {"quantity_id": "leave-as-is"}}],
        "invariants": [{"id": "i", "statement": "待核验"}],
        "consistency_groups": [{"id": "g"}],
    }
    assert architecture_runtime_view(source) == source


def test_none_missing_fields_and_unfamiliar_members_are_not_filled_or_dropped():
    assert architecture_runtime_view(None) is None
    source = {"components": [None, "owner note", {"contract": "缺少地址"}],
              "bindings": {"owner_note": "原样保留"}, "quantities": None}
    assert architecture_runtime_view(source) == source
    with pytest.raises(ArchitectureAddressError) as error:
        architecture_runtime_view([])
    assert error.value.path == "$"
