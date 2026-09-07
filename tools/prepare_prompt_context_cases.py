"""Prepare portable, labeled static evidence slices from read-only historical cases."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from geng_agent.prompt_identity import file_identity


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.out.resolve().is_relative_to(ROOT):
        parser.error("Generated datasets belong outside the checkout")
    source_manifest = []

    def read(path):
        source_manifest.append(file_identity(path))
        return path.read_text(encoding="utf-8-sig")

    def paper_chunks(case, ids=None):
        document = json.loads(read(case / "paper_chunks.json"))
        return "\n\n".join(f"[{chunk['chunk_id']}]\n{chunk.get('text', '')}" for chunk in document["chunks"]
                            if ids is None or chunk["chunk_id"] in ids)

    cases = []

    def add(case_id, origin, question, files, expected, writer_account="No independently verified Writer explanation is supplied."):
        task = {"task_id": "evidence_slice", "scientific_acceptance": {
            "core_conclusions": [{"claim_id": "slice", "statement": question}], "key_numeric_targets": []}}
        packet = {"task": task, "task_facts": {}, "question": question,
                  "evidence_files": list(files), "writer_account_path": "inputs/writer_account.json",
                  "scope": "Static slice only. No current-run certification, no hidden full dataset or omitted source is assumed."}
        files = dict(files)
        files["inputs/task_report_input.json"] = json.dumps(packet, ensure_ascii=False, indent=2)
        files["inputs/writer_account.json"] = json.dumps({"reported_explanation": writer_account}, ensure_ascii=False)
        # This is the actual legacy bundle shape: aliases repeat task/facts and
        # paper excerpts, while the original source entry remains available.
        excerpt = files.get("paper_evidence/source/excerpt.txt", "")
        evidence = {"task": task, "facts": {}, "paper_context": excerpt,
                    "paper_source": {"relative_path": "paper_evidence/source/excerpt.txt"}}
        files["paper_evidence/01_evidence_slice/evidence.json"] = json.dumps(evidence, ensure_ascii=False, indent=2)
        files["paper_evidence/01_evidence_slice/context.md"] = json.dumps(evidence, ensure_ascii=False, indent=2)
        files["paper_evidence/index.json"] = json.dumps({"tasks": [{
            "task_evidence_json": "paper_evidence/01_evidence_slice/evidence.json",
            "task_context_markdown": "paper_evidence/01_evidence_slice/context.md"}],
            "paper_source": {"relative_path": "paper_evidence/source/excerpt.txt"}}, indent=2)
        cases.append({"case_id": case_id, "origin": origin, "files": files, "expected": expected})

    owc = args.cases_root / "case_observe_owc_2504_02134_20260823"
    snapshot = owc / "audit/04a_task_reporters/01_reproduce_fig1_two_path_frequency_selectivity/round_003/inputs/writer_output"
    add("archived_owc_observable", "historical OWC source and measurements",
        "Does the submitted response implement the paper's Equation (6) observable faithfully, independently of whether its example peak looks similar?",
        {"paper_evidence/source/excerpt.txt": paper_chunks(owc),
         "inputs/source/task.py": read(snapshot / "source/tasks/reproduce_fig1_two_path_frequency_selectivity.py"),
         "inputs/source/response.py": read(snapshot / "source/src/channel/response.py"),
         "inputs/results/summary.json": read(snapshot / "outputs/summary.json")},
        {"claim_status": "unsupported", "rerun_allowed": True},
        "The submitted curve follows the paper and uses a disclosed CP projection and DFT normalization.")

    otfs = args.cases_root / "case_twc_otfs_multisat_20260719_023016"
    add("archived_otfs_approximation", "historical OTFS paper and CSV",
        "Do the recorded finite-dimensional and DE results support the paper's tight-approximation claim, particularly the 35 dBm point? Is a concrete code correction established by the supplied evidence?",
        {"paper_evidence/source/excerpt.txt": paper_chunks(otfs, {"p15_c1"}),
         "inputs/results/results.csv": read(otfs / "repro_project/outputs/reproduce_fig_8_jpl_scheduling/results.csv"),
         "inputs/config.json": read(otfs / "repro_project/configs/reproduce_fig_8_jpl_scheduling_config_smoke.json")},
        {"claim_status": "unsupported", "rerun_allowed": False})
    add("archived_otfs_uncertainty", "historical OTFS paper and two-realization CSV",
        "Do these two-realization results establish the expected high-power MSP versus SSP ordering? Distinguish lack of support from a proven expectation reversal; no specific code correction is supplied.",
        {"paper_evidence/source/excerpt.txt": paper_chunks(otfs, {"p13_c1"}),
         "inputs/results/results.csv": read(otfs / "repro_project/outputs/reproduce_fig_5_sum_rate_comparisons/results.csv"),
         "inputs/config.json": read(otfs / "repro_project/configs/reproduce_fig_5_sum_rate_comparisons_config_smoke.json")},
        {"claim_status": "unassessable_missing_information", "rerun_allowed": False})

    deepsc = args.cases_root / "deepsc_s_2102_12605_full_20260722_001"
    paper = json.loads(read(deepsc / "paper_chunks.json"))
    selected = [chunk for chunk in paper["chunks"] if "train" in chunk.get("text", "").lower()][:2]
    add("archived_deepsc_incomplete", "historical DeepSC-S paper; missing execution evidence",
        "Does the available evidence establish that a trained speech-semantic model was evaluated on held-out data and supports its claimed advantage? Only analysis materials are supplied, with no checkpoint, evaluation data, or execution receipt.",
        {"paper_evidence/source/excerpt.txt": "\n\n".join(f"[{chunk['chunk_id']}]\n{chunk['text']}" for chunk in selected),
         "inputs/available_materials.json": json.dumps({"available": ["paper excerpts"], "not_supplied": ["trained checkpoint", "evaluation outputs", "execution receipt"]})},
        {"claim_status": "unassessable_missing_information", "rerun_allowed": False})

    synthetic = json.loads((ROOT / "tests/fixtures/scientific_calibration/cases.json").read_text(encoding="utf-8"))
    expected = {"numeric_accuracy_a": ("supported", False), "numeric_accuracy_b": ("unsupported", True),
                "learned_method_b": ("unsupported", True)}
    for case in synthetic["cases"]:
        if case["case_id"] not in expected:
            continue
        status, rerun = expected[case["case_id"]]
        question = case["reviewer_question"]
        if case["case_id"] == "learned_method_b":
            question = "Does the implementation preserve the paper's learned-method identity, regardless of its plotted method ordering?"
        add("synthetic_" + case["case_id"], "synthetic scientific-calibration fixture, not a real paper run", question,
            {"paper_evidence/source/excerpt.txt": case["paper_excerpt"],
             "inputs/results/observations.txt": case["local_observation"],
             "inputs/source/inspection.txt": case["available_change"]},
            {"claim_status": status, "rerun_allowed": rerun})

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("x", encoding="utf-8") as stream:
        json.dump({"schema_version": "1.0", "scope": "fixed scientific evidence slices, not whole-case labels",
                   "source_manifest": source_manifest, "cases": cases}, stream, ensure_ascii=False, indent=2)
    print(json.dumps({"cases": len(cases), "dataset": str(args.out), "source_files": len(source_manifest)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
