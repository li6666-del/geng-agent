from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from geng_agent.agentic_analysis import CODEX_ANALYSIS_BACKEND
from geng_agent.facts_coverage import (
    compute_fact_coverage,
    compute_task_coverage,
    enumerate_paper_anchors,
    facts_referenced_anchors,
    merge_engineering_facts,
    merge_repro_tasks,
)
from geng_agent.pipeline import ReviewPipeline


def _chunk(text: str) -> dict:
    return {"chunk_id": "c1", "text": text, "page": 1, "section": "S"}


class EnumerateAnchorsTests(unittest.TestCase):
    def test_finds_figures_and_tables(self) -> None:
        anchors = enumerate_paper_anchors([_chunk("As shown in Fig. 7 and Figure 3, see Table II and Table 1.")])
        self.assertEqual(anchors["figures"], ["3", "7"])
        self.assertEqual(anchors["tables"], ["1", "II"])

    def test_figure_number_list_is_expanded(self) -> None:
        anchors = enumerate_paper_anchors([_chunk("Figs. 3 and 4, 5 compare the curves.")])
        self.assertEqual(anchors["figures"], ["3", "4", "5"])

    def test_subfigure_letters_remain_distinct(self) -> None:
        anchors = enumerate_paper_anchors([_chunk("Fig. 7a and Fig. 7b show two regimes.")])
        self.assertEqual(anchors["figures"], ["7:a", "7:b"])

    def test_year_like_number_is_not_a_figure(self) -> None:
        # "Fig. 2020" must not register figure 20 / 2 (two-digit cap + word boundary).
        anchors = enumerate_paper_anchors([_chunk("Published Fig. 2020 reference.")])
        self.assertEqual(anchors["figures"], [])

    def test_table_keyword_does_not_swallow_words(self) -> None:
        # "Table is shown" must NOT become Table I (roman branch is case-sensitive).
        anchors = enumerate_paper_anchors([_chunk("The Table is shown below and configuration matters.")])
        self.assertEqual(anchors["tables"], [])
        self.assertEqual(anchors["figures"], [])

    def test_configuration_is_not_a_figure(self) -> None:
        anchors = enumerate_paper_anchors([_chunk("The configuration uses 3 antennas.")])
        self.assertEqual(anchors["figures"], [])


class FactsReferencedTests(unittest.TestCase):
    def test_anchor_read_from_multiple_fact_fields(self) -> None:
        facts = [
            {"name": "Fig.7 sum-rate", "type": "figure_claim", "value": {}, "source": {"figure_ref": "", "quote": "", "section": ""}},
            {"name": "axis", "type": "figure_claim", "value": {}, "source": {"figure_ref": "Figure 3 BER vs SNR", "quote": "", "section": ""}},
            {"name": "cell", "type": "metric", "value": {"note": "Table II row 2"}, "source": {"figure_ref": "", "quote": "", "section": ""}},
        ]
        ref = facts_referenced_anchors(facts)
        self.assertEqual(ref["figures"], {"7", "3"})
        self.assertEqual(ref["tables"], {"II"})

    def test_bare_number_does_not_cover_anchor(self) -> None:
        # a value of "7" with no fig/table keyword must not count as covering figure 7
        facts = [{"name": "threshold", "type": "simulation_parameter", "value": {"v": 7}, "source": {"figure_ref": "", "quote": "", "section": ""}}]
        ref = facts_referenced_anchors(facts)
        self.assertEqual(ref["figures"], set())


class CoverageTests(unittest.TestCase):
    def test_uncovered_anchor_is_reported(self) -> None:
        chunks = [_chunk("Results in Fig. 3 and Fig. 7; parameters in Table I.")]
        facts = [{"name": "Fig.3 curve", "type": "figure_claim", "value": {}, "source": {"figure_ref": "", "quote": "", "section": ""}}]
        cov = compute_fact_coverage(chunks, facts)
        self.assertEqual(cov["paper_figures"], ["3", "7"])
        self.assertEqual(cov["covered_figures"], ["3"])
        self.assertEqual(cov["uncovered_figures"], ["7"])
        self.assertEqual(cov["uncovered_tables"], ["I"])
        self.assertFalse(cov["fully_covered"])

    def test_fully_covered(self) -> None:
        chunks = [_chunk("See Fig. 1.")]
        facts = [{"name": "Fig.1 plot", "type": "figure_claim", "value": {}, "source": {"figure_ref": "", "quote": "", "section": ""}}]
        cov = compute_fact_coverage(chunks, facts)
        self.assertTrue(cov["fully_covered"])
        self.assertEqual(cov["uncovered_figures"], [])


class MergeTests(unittest.TestCase):
    def _doc(self, facts, missing=None) -> dict:
        return {"paper_domain": "communication", "paper_repro_type": "other", "engineering_facts": facts, "missing_information": missing or []}

    def test_dedup_by_type_and_name(self) -> None:
        base = self._doc([{"type": "simulation_parameter", "name": "SNR range"}])
        addition = self._doc([
            {"type": "simulation_parameter", "name": "snr  range"},  # same after normalization -> drop
            {"type": "metric", "name": "BER"},                        # new -> keep
        ])
        merged, added = merge_engineering_facts(base, addition)
        self.assertEqual(added, 1)
        self.assertEqual(len(merged["engineering_facts"]), 2)
        names = {f["name"] for f in merged["engineering_facts"]}
        self.assertEqual(names, {"SNR range", "BER"})

    def test_idempotent_second_merge_adds_zero(self) -> None:
        base = self._doc([{"type": "metric", "name": "BER"}])
        addition = self._doc([{"type": "channel_model", "name": "Rayleigh"}])
        merged, added1 = merge_engineering_facts(base, addition)
        merged2, added2 = merge_engineering_facts(merged, addition)
        self.assertEqual(added1, 1)
        self.assertEqual(added2, 0)
        self.assertEqual(len(merged2["engineering_facts"]), 2)

    def test_missing_information_merged_by_name(self) -> None:
        base = self._doc([], missing=[{"name": "seed", "why_needed": "x", "impact": "high"}])
        addition = self._doc([], missing=[
            {"name": "seed", "why_needed": "dup", "impact": "low"},      # dup name -> drop
            {"name": "code length", "why_needed": "y", "impact": "high"},  # new -> keep
        ])
        merged, _ = merge_engineering_facts(base, addition)
        names = [m["name"] for m in merged["missing_information"]]
        self.assertEqual(names, ["seed", "code length"])

    def test_base_facts_preserved_and_not_mutated(self) -> None:
        base_facts = [{"type": "metric", "name": "BER"}]
        base = self._doc(base_facts)
        merge_engineering_facts(base, self._doc([{"type": "metric", "name": "SER"}]))
        # original list object must be untouched
        self.assertEqual(len(base_facts), 1)


class _GapLLM:
    """Returns one NEW fact on the first gap pass, then nothing -> loop must terminate."""

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, prompt: str, *, system=None, response_format=None) -> str:
        self.calls += 1
        if self.calls == 1:
            return json.dumps({
                "paper_domain": "communication",
                "paper_repro_type": "other",
                "engineering_facts": [{
                    "type": "simulation_parameter",
                    "name": "alpha_ST_threshold",
                    "value": {"v": 0.5},
                    "source": {"source_kind": "text", "chunk_id": "c1", "page": 1,
                               "section": "S", "quote": "alpha_ST=0.5 used for Fig. 7", "figure_ref": ""},
                    "confidence": "high",
                    "used_for_reproduction": True,
                }],
                "missing_information": [],
            })
        return json.dumps({
            "paper_domain": "communication", "paper_repro_type": "other",
            "engineering_facts": [], "missing_information": [],
        })


class _AlwaysNewGapLLM:
    """Returns one unique fact every time, so the gap loop can only stop at max_rounds."""

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, prompt: str, *, system=None, response_format=None) -> str:
        self.calls += 1
        return json.dumps({
            "paper_domain": "communication",
            "paper_repro_type": "other",
            "engineering_facts": [{
                "type": "simulation_parameter",
                "name": f"gap_fact_{self.calls}",
                "value": {"round": self.calls},
                "source": {"source_kind": "text", "chunk_id": "c1", "page": 1,
                           "section": "S", "quote": f"gap fact {self.calls}", "figure_ref": ""},
                "confidence": "high",
                "used_for_reproduction": True,
            }],
            "missing_information": [],
        })


class GapFinderIntegrationTests(unittest.TestCase):
    def test_gap_finder_adds_missing_fact_merges_and_terminates(self) -> None:
        base = {
            "paper_domain": "communication",
            "paper_repro_type": "other",
            "engineering_facts": [{
                "type": "figure_claim", "name": "Fig.7 sum-rate", "value": {},
                "source": {"source_kind": "text", "chunk_id": "c1", "page": 1,
                           "section": "S", "quote": "Fig. 7 shows sum-rate", "figure_ref": ""},
                "confidence": "high", "used_for_reproduction": True,
            }],
            "missing_information": [],
        }
        paper = {"chunks": [{"chunk_id": "c1", "text": "Fig. 7 sum-rate vs power with threshold alpha_ST.", "page": 1, "section": "S"}]}

        with TemporaryDirectory() as d:
            out_dir = Path(d)
            (out_dir / "audit").mkdir()
            client = _GapLLM()
            pipe = ReviewPipeline(client=client)
            result = pipe._augment_facts_with_gap_finder(
                facts=base, paper=paper, paper_context="ctx", paper_images=[],
                valid_chunk_ids={"c1"}, valid_pages=set(),
                output_dir=out_dir, audit_dir=out_dir / "audit",
                resume=False, max_attempts=2, max_rounds=3,
            )

            names = {f["name"] for f in result["engineering_facts"]}
            self.assertIn("alpha_ST_threshold", names)   # gap fact added
            self.assertIn("Fig.7 sum-rate", names)       # base fact preserved
            # Round 1 adds one fact; the first zero-addition round confirms convergence.
            self.assertEqual(client.calls, 2)
            written = json.loads((out_dir / "engineering_facts.json").read_text(encoding="utf-8"))
            self.assertEqual(written["_meta"]["gap_finder"]["round_1_added"], 1)
            self.assertEqual(written["_meta"]["gap_finder"]["round_2_added"], 0)
            self.assertEqual(written["_meta"]["gap_finder"]["stop_reason"], "semantic_dry_round")

    def test_gap_finder_runs_until_six_round_cap_when_always_adding(self) -> None:
        base = {
            "paper_domain": "communication",
            "paper_repro_type": "other",
            "engineering_facts": [],
            "missing_information": [],
        }
        paper = {"chunks": [{"chunk_id": "c1", "text": "Fig. 1 reports a curve.", "page": 1, "section": "S"}]}

        with TemporaryDirectory() as d:
            out_dir = Path(d)
            (out_dir / "audit").mkdir()
            client = _AlwaysNewGapLLM()
            pipe = ReviewPipeline(client=client)
            result = pipe._augment_facts_with_gap_finder(
                facts=base, paper=paper, paper_context="ctx", paper_images=[],
                valid_chunk_ids={"c1"}, valid_pages=set(),
                output_dir=out_dir, audit_dir=out_dir / "audit",
                resume=False, max_attempts=1, max_rounds=6,
            )

            self.assertEqual(client.calls, 6)
            self.assertEqual(len(result["engineering_facts"]), 6)
            self.assertEqual(result["_meta"]["gap_finder"]["rounds_run"], 6)
            self.assertEqual(result["_meta"]["gap_finder"]["max_rounds"], 6)
            self.assertEqual(result["_meta"]["gap_finder"]["stop_reason"], "explicit_test_limit")

    def test_zero_rounds_is_a_noop(self) -> None:
        base = {"paper_domain": "communication", "paper_repro_type": "other",
                "engineering_facts": [{"type": "metric", "name": "BER"}], "missing_information": []}
        client = _GapLLM()
        pipe = ReviewPipeline(client=client)
        with TemporaryDirectory() as d:
            out = pipe._augment_facts_with_gap_finder(
                facts=base, paper={"chunks": []}, paper_context="", paper_images=[],
                valid_chunk_ids=set(), valid_pages=set(),
                output_dir=Path(d), audit_dir=Path(d), resume=False, max_attempts=1, max_rounds=0,
            )
        self.assertEqual(client.calls, 0)
        self.assertEqual(out, base)

    def test_codex_gap_finder_uses_one_specialist_and_stops_after_deduped_dry_round(self) -> None:
        base = {
            "paper_domain": "communication",
            "paper_repro_type": "other",
            "engineering_facts": [],
            "missing_information": [],
        }
        paper = {"chunks": [{"chunk_id": "c1", "text": "Fig. 7 shows BER versus SNR.", "page": 1, "section": "S"}]}
        gap_fact = {
            "type": "figure_claim",
            "name": "Figure 7 BER",
            "value": {},
            "source": {"source_kind": "text", "chunk_id": "c1", "page": 1, "section": "S", "quote": "Fig. 7 shows BER", "figure_ref": ""},
            "confidence": "high",
            "used_for_reproduction": True,
        }
        returned_docs = [
            {"paper_domain": "communication", "paper_repro_type": "other", "engineering_facts": [gap_fact], "missing_information": []},
            {"paper_domain": "communication", "paper_repro_type": "other", "engineering_facts": [gap_fact], "missing_information": []},
            {"paper_domain": "communication", "paper_repro_type": "other", "engineering_facts": [gap_fact], "missing_information": []},
        ]
        calls: list[dict] = []

        def fake_gap_stage(**kwargs):
            calls.append(kwargs)
            return returned_docs[len(calls) - 1]

        with TemporaryDirectory() as d:
            out_dir = Path(d)
            (out_dir / "audit").mkdir()
            pipe = ReviewPipeline(client=None)
            with patch.object(pipe, "_load_or_create_analysis_stage_json", side_effect=fake_gap_stage):
                result = pipe._augment_facts_with_gap_finder(
                    facts=base,
                    paper=paper,
                    paper_context="ctx",
                    paper_images=[],
                    valid_chunk_ids={"c1"},
                    valid_pages=set(),
                    output_dir=out_dir,
                    audit_dir=out_dir / "audit",
                    resume=False,
                    max_attempts=1,
                    max_rounds=6,
                    analysis_backend=CODEX_ANALYSIS_BACKEND,
                )

        self.assertEqual(len(calls), 2)
        self.assertTrue(all("agent_width" not in call for call in calls))
        self.assertEqual(len(result["engineering_facts"]), 1)
        self.assertEqual(result["_meta"]["gap_finder"]["round_1_added"], 1)
        self.assertEqual(result["_meta"]["gap_finder"]["round_2_added"], 0)
        self.assertEqual(result["_meta"]["gap_finder"]["stop_reason"], "semantic_dry_round")


def _fclaim(name: str) -> dict:
    return {"type": "figure_claim", "name": name, "value": {},
            "source": {"source_kind": "text", "chunk_id": "c1", "page": 1, "section": "R", "quote": name, "figure_ref": ""},
            "confidence": "high", "used_for_reproduction": True}


class TaskCoverageTests(unittest.TestCase):
    def test_uncovered_experiment_reported_and_diagram_excluded(self) -> None:
        facts = {"engineering_facts": [
            _fclaim("Figure 4: Empirical CDF of UPA ZF sum rate"),  # experiment (cdf/sum rate)
            _fclaim("Figure 2: STAB system model with L=3"),        # diagram -> excluded
            _fclaim("Figure 7: Average sum rate vs transmit power"),  # experiment
        ]}
        tasks = {"repro_tasks": [{"task_id": "reproduce_fig_4", "figure_or_claim": "Fig. 4", "target": "CDF"}]}
        cov = compute_task_coverage(facts, tasks)
        self.assertEqual(cov["experiment_figures"], ["4", "7"])  # Fig 2 (diagram) not an experiment
        self.assertEqual(cov["uncovered_figures"], ["7"])
        self.assertFalse(cov["fully_covered"])

    def test_fully_covered(self) -> None:
        facts = {"engineering_facts": [_fclaim("Figure 1: BER vs SNR curve")]}
        tasks = {"repro_tasks": [{"task_id": "t1", "figure_or_claim": "Figure 1", "target": "BER"}]}
        self.assertTrue(compute_task_coverage(facts, tasks)["fully_covered"])

    def test_concept_figure_is_not_an_experiment(self) -> None:
        # positive-evidence: a concept/geometry illustration (no measurable metric) is NOT an
        # experiment, so it never enters the gap worklist (the Fig.1 misjudgement).
        facts = {"engineering_facts": [
            _fclaim("Fig. 1 concept figure"),  # geometry illustration -> excluded
            _fclaim("Figure 7: average sum rate versus transmit power"),  # result -> experiment
        ]}
        cov = compute_task_coverage(facts, {"repro_tasks": []})
        self.assertEqual(cov["experiment_figures"], ["7"])
        self.assertNotIn("1", cov["experiment_figures"])


class ConcreteTaskGateTests(unittest.TestCase):
    def test_metric_other_rejected(self) -> None:
        from geng_agent.facts_coverage import is_concrete_experiment_task
        self.assertFalse(is_concrete_experiment_task({"metric": "other", "output_columns": ["x"]}))

    def test_empty_columns_rejected(self) -> None:
        from geng_agent.facts_coverage import is_concrete_experiment_task
        self.assertFalse(is_concrete_experiment_task({"metric": "bit_error_rate", "output_columns": []}))

    def test_concrete_metric_with_columns_accepted(self) -> None:
        from geng_agent.facts_coverage import is_concrete_experiment_task
        self.assertTrue(is_concrete_experiment_task({"metric": "throughput", "output_columns": ["snr_db", "rate"]}))


class MergeReproTasksTests(unittest.TestCase):
    def test_dedup_by_figure_and_task_id(self) -> None:
        base = {"repro_tasks": [{"task_id": "reproduce_fig_4", "figure_or_claim": "Fig. 4"}]}
        addition = {"repro_tasks": [
            {"task_id": "reproduce_fig_4b", "figure_or_claim": "Fig. 4"},  # same experiment -> drop
            {"task_id": "reproduce_fig_4", "figure_or_claim": "Fig. 9"},   # same task_id -> drop
            {"task_id": "reproduce_fig_7", "figure_or_claim": "Fig. 7"},   # new -> keep
        ]}
        merged, added = merge_repro_tasks(base, addition)
        self.assertEqual(added, 2)  # one new task plus one newly recorded task-id conflict
        self.assertEqual({t["figure_or_claim"] for t in merged["repro_tasks"]}, {"Fig. 4", "Fig. 7"})


class _GapTaskLLM:
    """Returns a task for the uncovered Fig.7 on the first gap call."""

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, prompt: str, *, system=None, response_format=None) -> str:
        self.calls += 1
        return json.dumps({"repro_tasks": [{
            "task_id": "reproduce_fig_7",
            "target": "sum rate vs transmit power",
            "metric": "spectral_efficiency",
            "metric_formula": "spectral_efficiency = log2(1+SINR)",
            "figure_or_claim": "Fig. 7",
            "expected_artifacts": ["outputs/results.csv", "outputs/fig7.png", "outputs/summary.json"],
            "output_columns": ["transmit_power_dbm", "sum_rate"],
            "expected_trend": {"x_axis": "transmit_power_dbm", "y_axis": "sum_rate", "direction": "increasing", "reason": "more power -> higher rate"},
            "comparison": {"baselines": ["ZF"], "curve_groups": ["STAB+SDS"], "tolerance": "qualitative trend"},
            "required_facts": [{"type": "figure_claim", "name": "Figure 7: Average sum rate vs transmit power"}],
            "assumptions": [],
            "risk_if_unreproducible": "core sum-rate curve cannot be checked",
        }]})


class TasksGapFinderIntegrationTests(unittest.TestCase):
    def test_gap_finder_adds_missing_task_and_stops_when_covered(self) -> None:
        facts = {"paper_domain": "communication", "paper_repro_type": "signal_chain", "engineering_facts": [
            _fclaim("Figure 4: Empirical CDF of sum rate"),
            _fclaim("Figure 7: Average sum rate vs transmit power"),
        ], "missing_information": []}
        base = {"repro_tasks": [{
            "task_id": "reproduce_fig_4", "target": "CDF of sum rate", "metric": "spectral_efficiency",
            "metric_formula": "spectral_efficiency = log2(1+SINR)", "figure_or_claim": "Fig. 4",
            "expected_artifacts": ["outputs/results.csv", "outputs/fig4.png", "outputs/summary.json"],
            "output_columns": ["sum_rate"],
            "expected_trend": {"x_axis": "sum_rate", "y_axis": "cdf", "direction": "increasing", "reason": "cdf"},
            "comparison": {"baselines": ["ZF"], "curve_groups": ["UPA"], "tolerance": "qualitative trend"},
            "required_facts": [{"type": "figure_claim", "name": "Figure 4: Empirical CDF of sum rate"}],
            "assumptions": [], "risk_if_unreproducible": "core",
        }]}
        with TemporaryDirectory() as d:
            out_dir = Path(d)
            (out_dir / "audit").mkdir()
            client = _GapTaskLLM()
            pipe = ReviewPipeline(client=client)
            result = pipe._augment_tasks_with_gap_finder(
                tasks=base, facts=facts, paper_context="ctx",
                output_dir=out_dir, audit_dir=out_dir / "audit",
                resume=False, max_attempts=2, max_rounds=3, tasks_timeout=120.0,
            )
            figs = {t["figure_or_claim"] for t in result["repro_tasks"]}
            self.assertEqual(figs, {"Fig. 4", "Fig. 7"})   # gap task added
            self.assertEqual(client.calls, 2)              # one add, then one zero-addition confirmation
            self.assertEqual(result["_meta"]["gap_finder"]["round_1_added"], 1)
            self.assertEqual(result["_meta"]["gap_finder"]["stop_reason"], "semantic_dry_round")
            self.assertTrue((out_dir / "repro_tasks.json").exists())

    def test_no_gap_call_when_already_fully_covered(self) -> None:
        facts = {"engineering_facts": [_fclaim("Figure 1: BER vs SNR")], "missing_information": []}
        base = {"repro_tasks": [{"task_id": "t1", "figure_or_claim": "Figure 1", "target": "BER"}]}
        client = _GapTaskLLM()
        pipe = ReviewPipeline(client=client)
        with TemporaryDirectory() as d:
            (Path(d) / "audit").mkdir()
            pipe._augment_tasks_with_gap_finder(
                tasks=base, facts=facts, paper_context="", output_dir=Path(d), audit_dir=Path(d) / "audit",
                resume=False, max_attempts=2, max_rounds=3, tasks_timeout=120.0,
            )
        self.assertEqual(client.calls, 2)  # coverage no longer bypasses the task-design expert

    def test_codex_task_gap_finder_uses_one_specialist(self) -> None:
        facts = {"paper_domain": "communication", "paper_repro_type": "signal_chain", "engineering_facts": [
            _fclaim("Figure 4: Empirical CDF of sum rate"),
            _fclaim("Figure 7: Average sum rate vs transmit power"),
        ], "missing_information": []}
        base = {"repro_tasks": [{
            "task_id": "reproduce_fig_4", "target": "CDF of sum rate", "metric": "spectral_efficiency",
            "metric_formula": "spectral_efficiency = log2(1+SINR)", "figure_or_claim": "Fig. 4",
            "expected_artifacts": ["outputs/results.csv", "outputs/fig4.png", "outputs/summary.json"],
            "output_columns": ["sum_rate"],
            "expected_trend": {"x_axis": "sum_rate", "y_axis": "cdf", "direction": "increasing", "reason": "cdf"},
            "comparison": {"baselines": ["ZF"], "curve_groups": ["UPA"], "tolerance": "qualitative trend"},
            "required_facts": [{"type": "figure_claim", "name": "Figure 4: Empirical CDF of sum rate"}],
            "assumptions": [], "risk_if_unreproducible": "core",
        }]}
        calls: list[dict] = []

        def fake_gap_stage(**kwargs):
            calls.append(kwargs)
            return {"repro_tasks": [{
                "task_id": "reproduce_fig_7",
                "target": "sum rate vs transmit power",
                "metric": "spectral_efficiency",
                "metric_formula": "spectral_efficiency = log2(1+SINR)",
                "figure_or_claim": "Fig. 7",
                "expected_artifacts": ["outputs/results.csv", "outputs/fig7.png", "outputs/summary.json"],
                "output_columns": ["transmit_power_dbm", "sum_rate"],
                "expected_trend": {"x_axis": "transmit_power_dbm", "y_axis": "sum_rate", "direction": "increasing", "reason": "more power -> higher rate"},
                "comparison": {"baselines": ["ZF"], "curve_groups": ["STAB+SDS"], "tolerance": "qualitative trend"},
                "required_facts": [{"type": "figure_claim", "name": "Figure 7: Average sum rate vs transmit power"}],
                "assumptions": [],
                "risk_if_unreproducible": "core sum-rate curve cannot be checked",
            }]}

        with TemporaryDirectory() as d:
            out_dir = Path(d)
            (out_dir / "audit").mkdir()
            pipe = ReviewPipeline(client=None)
            with patch.object(pipe, "_load_or_create_analysis_stage_json", side_effect=fake_gap_stage):
                result = pipe._augment_tasks_with_gap_finder(
                    tasks=base,
                    facts=facts,
                    paper_context="ctx",
                    output_dir=out_dir,
                    audit_dir=out_dir / "audit",
                    resume=False,
                    max_attempts=1,
                    max_rounds=6,
                    tasks_timeout=120.0,
                    analysis_backend=CODEX_ANALYSIS_BACKEND,
                )

        self.assertEqual(len(calls), 2)
        self.assertTrue(all("agent_width" not in call for call in calls))
        self.assertEqual({task["figure_or_claim"] for task in result["repro_tasks"]}, {"Fig. 4", "Fig. 7"})


class _OtherMetricGapLLM:
    """Returns a task for the uncovered Fig.7 but with metric=other -> the gate must reject it."""

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, prompt: str, *, system=None, response_format=None) -> str:
        self.calls += 1
        return json.dumps({"repro_tasks": [{
            "task_id": "reproduce_fig_7",
            "target": "sum rate vs power",
            "metric": "other",  # non-reproducible signal -> deterministic gate rejects
            "metric_formula": "n/a",
            "figure_or_claim": "Fig. 7",
            "expected_artifacts": ["outputs/results.csv", "outputs/x.png", "outputs/summary.json"],
            "output_columns": ["x"],
            "expected_trend": {"x_axis": "p", "y_axis": "r", "direction": "increasing", "reason": "r"},
            "comparison": {"baselines": ["ZF"], "curve_groups": ["A"], "tolerance": "qualitative"},
            "required_facts": [{"type": "figure_claim", "name": "Figure 7: average sum rate versus transmit power"}],
            "assumptions": [],
            "risk_if_unreproducible": "x",
        }]})


class TaskGapMetricGateTests(unittest.TestCase):
    def test_metric_other_gap_task_is_rejected(self) -> None:
        facts = {"paper_domain": "communication", "paper_repro_type": "signal_chain", "engineering_facts": [
            _fclaim("Figure 4: Empirical CDF of sum rate"),
            _fclaim("Figure 7: average sum rate versus transmit power"),
        ], "missing_information": []}
        base = {"repro_tasks": [{
            "task_id": "reproduce_fig_4", "target": "CDF", "metric": "spectral_efficiency",
            "metric_formula": "se = log2(1+SINR)", "figure_or_claim": "Fig. 4",
            "expected_artifacts": ["outputs/results.csv", "outputs/f.png", "outputs/summary.json"],
            "output_columns": ["sum_rate"],
            "expected_trend": {"x_axis": "sum_rate", "y_axis": "cdf", "direction": "increasing", "reason": "cdf"},
            "comparison": {"baselines": ["ZF"], "curve_groups": ["UPA"], "tolerance": "qualitative"},
            "required_facts": [{"type": "figure_claim", "name": "Figure 4: Empirical CDF of sum rate"}],
            "assumptions": [], "risk_if_unreproducible": "core",
        }]}
        with TemporaryDirectory() as d:
            (Path(d) / "audit").mkdir()
            client = _OtherMetricGapLLM()
            pipe = ReviewPipeline(client=client)
            result = pipe._augment_tasks_with_gap_finder(
                tasks=base, facts=facts, paper_context="ctx",
                output_dir=Path(d), audit_dir=Path(d) / "audit",
                resume=False, max_attempts=2, max_rounds=3, tasks_timeout=120.0,
            )
            figs = {t["figure_or_claim"] for t in result["repro_tasks"]}
            self.assertEqual(figs, {"Fig. 4"})  # the metric=other Fig.7 task was rejected, not added
            self.assertEqual(result["_meta"]["gap_finder"]["stop_reason"], "semantic_dry_round")
            self.assertTrue((Path(d) / "audit" / "tasks_gap_round_1_rejected.json").exists())


if __name__ == "__main__":
    unittest.main()
