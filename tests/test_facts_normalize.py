from copy import deepcopy
import pytest
from geng_agent.facts_normalize import (
    finalize_engineering_facts, normalize_engineering_facts_candidate,
    select_valid_engineering_facts, recover_truncated_engineering_facts,
)
from geng_agent.schemas import validate_stage


def test_facts_keep_unknown_terms_values_provenance_and_fields():
    source = {"engineering_facts": [{"type": "论文自定义指标", "name": "x", "value": -1,
        "confidence": "作者明确给出", "source": {"page": 999, "printed": "x=-1"}, "extra": {"condition": "log"}},
        {"type": "parameter", "name": "x", "value": 10}], "missing_information": [{"name": "unknown"}],
        "additional_science": {"range": [0, 3, 6]}}
    original = deepcopy(source)
    copied = finalize_engineering_facts(source, {"c1"}, {1})
    assert copied == original == source
    assert select_valid_engineering_facts(source)[0] == source["engineering_facts"]
    assert validate_stage("engineering_facts", copied) == []
    copied["engineering_facts"][0]["value"] = 99
    assert source == original


def test_invalid_container_is_not_replaced_with_empty_science():
    with pytest.raises(ValueError):
        normalize_engineering_facts_candidate(["not an object"])
    assert validate_stage("engineering_facts", {"engineering_facts": {"x": 1}})


def test_truncated_prefix_is_not_a_completed_fact_document():
    assert recover_truncated_engineering_facts('{"engineering_facts":[{"name":"first"},') is None
    assert validate_stage("engineering_facts", {"engineering_facts": []}) == []
