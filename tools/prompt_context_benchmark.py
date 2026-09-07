"""Explicit, bounded static-evidence comparisons; never executes scientific code.

Dataset labels and original source locations remain outside worker directories.
Each arm uses the production Reporter's scientific instructions, with the same
slice-only answer contract. This is not an end-to-end reproduction benchmark.
"""
from __future__ import annotations

import argparse
import ast
from concurrent.futures import ThreadPoolExecutor, as_completed
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from geng_agent.codex_runner import run_codex_subprocess
from geng_agent.json_utils import parse_json_object
from geng_agent.prompt_identity import file_identity, text_identity


def reporter_scientific_prompt(revision: str | None) -> str:
    import geng_agent.task_reporter_context as module
    if revision is None:
        builder = module._build_task_reporter_brief
    else:
        source = subprocess.check_output(["git", "show", f"{revision}:geng_agent/task_reporter_context.py"], cwd=ROOT).decode("utf-8")
        tree = ast.parse(source)
        selected = [node for node in tree.body if (
            isinstance(node, ast.FunctionDef) and node.name == "_build_task_reporter_brief") or (
            isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id in {
                "TASK_VERIFICATION_FILE", "REPORTER_CONVERGENCE_POLICY"} for target in node.targets))]
        namespace = dict(vars(module))
        exec(compile(ast.Module(body=selected, type_ignores=[]), "<baseline reporter>", "exec"), namespace)
        builder = namespace["_build_task_reporter_brief"]
    prompt = builder(task_id="evidence_slice", report_asset_dir="report_assets/evidence_slice", include_all_paper_pages=False)
    # Output handling is held constant; only scientific instruction content differs.
    return prompt.split("## Output", 1)[0]


SLICE_CONTRACT = """
This is a fixed evidence-slice comparison, not certification of a complete project
or a new scientific run. Judge only the question in inputs/task_report_input.json.
Only supplied files are in scope. Do not read outside this workspace, execute
scientific source, install dependencies, or use the network. No images are attached;
the supplied text/source/data identify what is and is not available.
Return one JSON object in your final answer, without writing report files:
{"claim_status":"supported|unsupported|unassessable_missing_information",
 "rerun_allowed":false,"method_identity":"faithful|changed|unknown",
 "evidence_files":[],"reason":"evidence-grounded explanation",
 "missing_evidence":[],"causal_correction":"specific justified correction or empty"}
A proposed diagnostic is not permission for another unchanged full experiment.
Do not infer the expected label from a scenario identifier.
"""


def tool_counts(status: dict) -> dict:
    path = status.get("full_transcript")
    if not path:
        return {"complete_transcript": False, "tool_calls": None, "tool_output_characters": None}
    items = {}
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            item = event.get("item") if isinstance(event, dict) else None
            if isinstance(item, dict) and item.get("type") in {"command_execution", "mcp_tool_call"}:
                items[str(item.get("id", len(items)))] = item
    return {"complete_transcript": True, "tool_calls": len(items),
            "tool_output_characters": sum(len(str(item.get("aggregated_output") or item.get("result") or "")) for item in items.values())}


def run_one(root: Path, case: dict, arm: str, scientific_prompt: str) -> dict:
    opaque_id = hashlib.sha256(str(case["case_id"]).encode()).hexdigest()[:12]
    workspace = root / "workers" / f"{opaque_id}_{arm}"
    workspace.mkdir(parents=True, exist_ok=False)
    files = dict(case["files"])
    # Both arms receive exactly the same evidence bytes. Context reduction is
    # measured separately; it must not be confounded with changed instructions.
    for relative, body in files.items():
        path = (workspace / relative).resolve()
        if not path.is_relative_to(workspace.resolve()):
            raise ValueError("Dataset path escapes worker workspace")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8", newline="")
    if arm == "candidate_deduplicated":
        from geng_agent.task_reporter_context import _canonicalize_reporter_paper_evidence
        _canonicalize_reporter_paper_evidence(workspace)
    prompt = scientific_prompt + "\n" + SLICE_CONTRACT
    status = run_codex_subprocess(role="task_reporter", work_dir=workspace, prompt=prompt,
        audit_dir=root / "audit" / f"{opaque_id}_{arm}", label="review", sandbox="read-only")
    raw = Path(status["last_message_path"]).read_text(encoding="utf-8") if status.get("last_message_path") and Path(status["last_message_path"]).is_file() else ""
    try:
        answer = parse_json_object(raw)
    except Exception:
        answer = None
    expected = case["expected"]
    assessed = isinstance(answer, dict) and answer.get("claim_status") in {
        "supported", "unsupported", "unassessable_missing_information"}
    row = {"case_id": case["case_id"], "origin": case["origin"], "arm": arm,
           "scientific_prompt_sha256": text_identity(scientific_prompt),
           "answer": answer, "assessed": assessed, "expected": expected,
           "claim_correct": answer.get("claim_status") == expected["claim_status"] if assessed else None,
           "false_success": assessed and answer.get("claim_status") == "supported" and expected["claim_status"] != "supported",
           "unnecessary_rerun": assessed and answer.get("rerun_allowed") is True and expected["rerun_allowed"] is False,
           "rerun_correct": answer.get("rerun_allowed") == expected["rerun_allowed"] if assessed else None,
           "usage": status.get("cost_event", {}).get("usage"), "duration_s": status.get("duration_s"),
           "input_manifest": status.get("input_manifest"), "status_path": str(root / "audit" / f"{opaque_id}_{arm}" / "review.json"),
           "workspace_input_bytes": sum(path.stat().st_size for path in workspace.rglob("*") if path.is_file()),
           **tool_counts(status)}
    target = root / "results" / f"{opaque_id}_{arm}.json"
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--baseline", default="9399d5c")
    parser.add_argument("--workers", type=int, choices=(1, 2, 3), default=2)
    parser.add_argument("--run", action="store_true", help="Explicitly enable actual model calls")
    parser.add_argument("--context-ablation", action="store_true", help="Add current production paper-evidence deduplication as a third arm")
    args = parser.parse_args()
    root = args.out.resolve()
    if root.is_relative_to(ROOT):
        parser.error("Model comparison outputs must be outside the repository")
    cases = json.loads(args.dataset.read_text(encoding="utf-8"))["cases"]
    if not 1 <= len(cases) <= 8:
        parser.error("Use 1-8 fixed cases per bounded comparison")
    prompts = {"baseline": reporter_scientific_prompt(args.baseline), "candidate": reporter_scientific_prompt(None)}
    if args.context_ablation:
        prompts["candidate_deduplicated"] = prompts["candidate"]
    root.mkdir(parents=True, exist_ok=False)
    plan = {"scope": "Fixed-evidence scientific instruction comparison; no scientific runs or end-to-end success claim.",
            "baseline_revision": args.baseline, "dataset": file_identity(args.dataset),
            "planned_calls": len(prompts) * len(cases), "evidence_identical_between_instruction_arms": True,
            "context_ablation": "production _canonicalize_reporter_paper_evidence only" if args.context_ablation else None,
            "prompt_hashes": {key: text_identity(value) for key, value in prompts.items()},
            "known_limitations": ["one sample per arm", "same account prefix caches may affect cost", "synthetic and historical slices are reported separately"]}
    (root / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    for arm, prompt in prompts.items():
        (root / f"{arm}_scientific_prompt.md").write_text(prompt, encoding="utf-8")
    if not args.run:
        print(json.dumps(plan, ensure_ascii=False))
        return
    arms = list(prompts)
    jobs = [(case, arm) for index, case in enumerate(cases) for arm in (arms[index % len(arms):] + arms[:index % len(arms)])]
    rows = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(run_one, root, case, arm, prompts[arm]): (case["case_id"], arm) for case, arm in jobs}
        for future in as_completed(futures):
            row = future.result()
            rows.append(row)
            print(json.dumps({key: row[key] for key in ("case_id", "arm", "assessed", "claim_correct", "rerun_correct", "duration_s")}), flush=True)
    summary = {"plan": plan, "rows": rows, "arms": {}}
    for arm in prompts:
        selected = [row for row in rows if row["arm"] == arm]
        summary["arms"][arm] = {"calls": len(selected), "assessed": sum(row["assessed"] for row in selected),
            "correct": sum(row["claim_correct"] is True for row in selected),
            "false_success": sum(bool(row["false_success"]) for row in selected),
            "unnecessary_reruns": sum(bool(row["unnecessary_rerun"]) for row in selected),
            "rerun_errors": sum(row["rerun_correct"] is False for row in selected),
            "duration_sum_s": sum(row["duration_s"] or 0 for row in selected),
            "usage_complete": all(row["usage"] is not None for row in selected),
            "observed_usage": {key: sum((row["usage"] or {}).get(key, 0) for row in selected)
                               for key in ("prompt_tokens", "cached_prompt_tokens", "completion_tokens", "total_tokens")}}
    (root / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
