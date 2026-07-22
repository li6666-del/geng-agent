import base64
import json
import os
import sys
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from geng_agent.agentic_task_reporters import (
    _accepted_asset_issues,
    _build_task_reporter_brief,
    _task_reporter_input_hash,
    run_codex_task_reporter_workflow,
    task_verifications_document,
)
from geng_agent.verification_result import (
    normalize_task_verification,
    partition_task_verification_issues,
    task_verification_issues,
)


PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="


def _fake_task_reporter(
    root: Path,
    *,
    verdict: str = "accepted",
    target: str = "none",
    misplaced_assets: bool = False,
    omit_paper_asset: bool = False,
) -> str:
    script = root / f"task_reporter_{verdict}_{target}.py"
    script.write_text(textwrap.dedent(f"""
        import base64
        import json
        import sys
        from pathlib import Path

        if sys.argv[1:] == ["exec", "--help"]:
            print("--ephemeral")
            raise SystemExit(0)

        root = Path.cwd()
        report_input = json.loads((root / "inputs" / "task_report_input.json").read_text(encoding="utf-8"))
        task_id = report_input["task_id"]
        asset_dir = root / ("misplaced_assets" if {misplaced_assets!r} else report_input["report_asset_dir"])
        result = {{
            "schema_version": "1.0",
            "task_id": task_id,
            "verdict": {verdict!r},
            "revision_target": {target!r},
            "comparison_summary": "direct comparison",
            "differences": [] if {verdict!r} == "accepted" else ["curve position differs"],
            "non_material_differences": [],
            "evidence_files": ["inputs/writer_output/outputs/curve.png"],
            "feedback": [] if {verdict!r} == "accepted" else ["adjust the model and run full"],
            "confidence": "high",
            "local_assets": [],
            "paper_assets": [],
            "remaining_uncertainties": []
        }}
        if {verdict!r} == "accepted":
            asset_dir.mkdir(parents=True)
            names = ("local_result.png",) if {omit_paper_asset!r} else ("local_result.png", "paper_target.png")
            for name in names:
                (asset_dir / name).write_bytes(base64.b64decode({PNG_B64!r}))
            result["local_assets"] = [str((asset_dir / "local_result.png").relative_to(root)).replace("\\\\", "/")]
            if not {omit_paper_asset!r}:
                result["paper_assets"] = [str((asset_dir / "paper_target.png").relative_to(root)).replace("\\\\", "/")]
        (root / "task_verification_result.json").write_text(json.dumps(result), encoding="utf-8")
    """), encoding="utf-8")
    return f'"{sys.executable}" "{script}"'


def _record(root: Path, task_id: str) -> dict:
    sandbox = root / f"sandbox_{task_id}"
    output = sandbox / "outputs" / task_id
    output.mkdir(parents=True)
    (output / "curve.png").write_bytes(base64.b64decode(PNG_B64))
    (output / "results.csv").write_text("x,y\\n0,1\\n", encoding="utf-8")
    tasks_dir = sandbox / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "__init__.py").write_text("", encoding="utf-8")
    (tasks_dir / f"{task_id}.py").write_text(f"TASK_ID = {task_id!r}\\n", encoding="utf-8")
    (tasks_dir / f"{task_id}_helper.py").write_text("HELPER = True\\n", encoding="utf-8")
    src_dir = sandbox / "src"
    src_dir.mkdir()
    (src_dir / "_io.py").write_text("def write_output(): pass\\n", encoding="utf-8")
    (src_dir / "simulation.py").write_text("SIMULATION = 'task-private'\\n", encoding="utf-8")
    (sandbox / "config.json").write_text('{"run_profile": "full"}\\n', encoding="utf-8")
    (sandbox / "config_smoke.json").write_text('{"run_profile": "smoke"}\\n', encoding="utf-8")
    (sandbox / "requirements.txt").write_text("numpy\\n", encoding="utf-8")
    (sandbox / "run_experiment.py").write_text("raise SystemExit(0)\\n", encoding="utf-8")
    (sandbox / "tasks_manifest.json").write_text('{"tasks": []}\\n', encoding="utf-8")
    return {
        "task_id": task_id,
        "sandbox": str(sandbox),
        "output_subdir": task_id,
        "writer_completed": True,
        "task_writer_status": "ready_for_review",
        "result_json": {
            "task_id": task_id,
            "status": "ready_for_review",
            "summary": "done",
            "local_image_paths": ["outputs/curve.png"],
            "execution_summary": {"full_run_count": 1, "last_returncode": 0},
        },
        "execution_summary": {"full_run_count": 1, "last_returncode": 0},
    }


class IsolatedTaskReporterTests(unittest.TestCase):
    def test_reporter_prompt_uses_core_claim_and_materiality_convergence_gate(self) -> None:
        prompt = _build_task_reporter_brief(
            task_id="task_a",
            report_asset_dir="report_assets/task_a",
            include_all_paper_pages=False,
        )

        self.assertIn("not to discover every conceivable imperfection", prompt)
        self.assertIn("Acceptance may be conditional on disclosed paper-silent assumptions", prompt)
        self.assertIn("Return `revise` to the Writer only when all of the following are true", prompt)
        self.assertIn("Do not issue speculative revisions", prompt)
        self.assertIn("the assigned core claim is supported", prompt)
        self.assertIn("inputs/writer_output/source/", prompt)
        self.assertIn("Inspect it statically; do not execute it or import it", prompt)
        self.assertIn("Trace paper-explicit equations", prompt)

    def test_accepted_verdict_allows_disclosed_assumptions_and_non_material_differences(self) -> None:
        result = {
            "schema_version": "1.0",
            "task_id": "task_a",
            "verdict": "accepted",
            "revision_target": "none",
            "comparison_summary": "Core ordering and trend match under a disclosed paper-silent seed assumption.",
            "differences": [],
            "non_material_differences": ["Exact marker positions differ slightly."],
            "evidence_files": ["inputs/writer_output/outputs/curve.png"],
            "feedback": [],
            "confidence": "medium",
            "remaining_uncertainties": ["The paper does not disclose its random seed."],
        }

        self.assertEqual(task_verification_issues(result, "task_a"), [])

    def test_non_scientific_verification_metadata_is_normalized(self) -> None:
        normalized = normalize_task_verification(
            {"task_id": "task_a", "verdict": "accepted", "differences": []},
            "task_a",
        )
        blockers, warnings = partition_task_verification_issues(normalized, "task_a")
        self.assertEqual(blockers, [])
        self.assertEqual(warnings, [])
        self.assertEqual(normalized["revision_target"], "none")
        self.assertEqual(normalized["confidence"], "medium")
        self.assertEqual(normalized["evidence_files"], ["inputs/task_report_input.json"])
    def test_pdf_fallback_crop_requires_exact_bbox_provenance(self) -> None:
        with TemporaryDirectory() as temp:
            workspace = Path(temp)
            asset_dir = workspace / "report_assets" / "task_a"
            asset_dir.mkdir(parents=True)
            for name in ("local_result.png", "paper_target.png"):
                (asset_dir / name).write_bytes(base64.b64decode(PNG_B64))
            verification = {
                "local_assets": ["report_assets/task_a/local_result.png"],
                "paper_assets": ["report_assets/task_a/paper_target.png"],
            }
            issues = _accepted_asset_issues(
                verification,
                workspace,
                "task_a",
                crop_result={
                    "status": "reporter_provided_crop",
                    "source_mode": "reporter_provided_crop",
                    "output_path": str(asset_dir / "paper_target.png"),
                    "output_sha256": "abc",
                    "selection_reason": "no_unique_verified_candidate",
                },
                require_verified_pdf_crop=True,
            )
            self.assertTrue(any("verified exact crop" in issue for issue in issues), issues)

    def _kwargs(self, root: Path, records: list[dict]) -> dict:
        paper = root / "paper.md"
        paper.write_text("Fig. 1 and Fig. 2.", encoding="utf-8")
        return {
            "paper": {"format": "markdown", "chunks": []},
            "paper_path": paper,
            "facts": {"engineering_facts": [
                {"type": "parameter", "name": "alpha_a", "value": "1"},
                {"type": "parameter", "name": "alpha_b", "value": "2"},
            ], "missing_information": []},
            "tasks": {"repro_tasks": [
                {"task_id": "task_a", "figure_or_claim": "Fig. 1", "required_facts": [{"type": "parameter", "name": "alpha_a"}]},
                {"task_id": "task_b", "figure_or_claim": "Fig. 2", "required_facts": [{"type": "parameter", "name": "alpha_b"}]},
            ]},
            "experiment_index": {"experiments": [{"task_id": "task_a", "source_pages": [1]}, {"task_id": "task_b", "source_pages": [2]}]},
            "paper_thesis": {}, "paper_images": [],
            "task_records": records, "output_dir": root / "case", "audit_dir": root / "case" / "audit",
            "timeout": 30, "resume": False,
        }

    def _one_kwargs(self, root: Path, records: list[dict], task_id: str) -> dict:
        batch = self._kwargs(root, records)
        task = next(item for item in batch["tasks"]["repro_tasks"] if item["task_id"] == task_id)
        return {
            "index": 1,
            "task": task,
            "task_record": next(item for item in records if item["task_id"] == task_id),
            "paper": batch["paper"], "paper_path": batch["paper_path"], "facts": batch["facts"],
            "experiment_index": batch["experiment_index"], "paper_thesis": batch["paper_thesis"],
            "paper_images": batch["paper_images"],
            "output_dir": batch["output_dir"], "audit_dir": batch["audit_dir"],
            "timeout": batch["timeout"], "resume": batch["resume"], "round_no": 1,
        }

    def test_task_reporter_receives_only_assigned_writer_output_and_copies_assets(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            records = [_record(root, "task_a"), _record(root, "task_b")]
            old = os.environ.get("GENG_CODEX_TASK_REPORTER_CMD")
            os.environ["GENG_CODEX_TASK_REPORTER_CMD"] = _fake_task_reporter(root)
            try:
                item = run_codex_task_reporter_workflow(**self._one_kwargs(root, records, "task_a"))
            finally:
                if old is None: os.environ.pop("GENG_CODEX_TASK_REPORTER_CMD", None)
                else: os.environ["GENG_CODEX_TASK_REPORTER_CMD"] = old
            self.assertTrue(item["ok"], item)
            self.assertTrue(item["accepted"])
            workspace = Path(item["workspace"])
            self.assertTrue((workspace / "inputs" / "writer_output" / "outputs" / "curve.png").is_file())
            source = workspace / "inputs" / "writer_output" / "source"
            self.assertTrue((source / "tasks" / "task_a.py").is_file())
            self.assertTrue((source / "tasks" / "task_a_helper.py").is_file())
            self.assertTrue((source / "src" / "_io.py").is_file())
            self.assertTrue((source / "config.json").is_file())
            self.assertFalse((source / "tasks" / "task_b.py").exists())
            task_input = json.loads((workspace / "inputs" / "task_report_input.json").read_text(encoding="utf-8"))
            self.assertEqual(task_input["task_id"], "task_a")
            self.assertEqual([item["name"] for item in task_input["task_facts"]["engineering_facts"]], ["alpha_a"])
            self.assertTrue(task_input["writer_source_available"])
            source_paths = {item["sandbox_relative_path"] for item in task_input["writer_source_files"]}
            self.assertIn("tasks/task_a.py", source_paths)
            self.assertIn("config.json", source_paths)
            self.assertNotIn("outputs/task_a/results.csv", source_paths)
            self.assertTrue((root / "case" / "report_assets" / "task_a" / "paper_target.png").is_file())

    def test_task_reporter_cache_hash_includes_writer_source_content(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            record = _record(root, "task_a")
            paper = root / "paper.md"
            paper.write_text("Fig. 1", encoding="utf-8")
            source = Path(record["sandbox"]) / "tasks" / "task_a.py"
            source.write_text("VALUE = 1\\n", encoding="utf-8")
            stat = source.stat()
            kwargs = {
                "task": {"task_id": "task_a"},
                "task_record": record,
                "paper_path": paper,
                "figure_candidates": [],
            }
            first = _task_reporter_input_hash(**kwargs)
            source.write_text("VALUE = 2\\n", encoding="utf-8")
            os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            second = _task_reporter_input_hash(**kwargs)

            self.assertEqual(source.stat().st_size, len("VALUE = 2\\n"))
            self.assertNotEqual(first, second)

    def test_revise_result_is_aggregated_and_routes_only_to_writer(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            records = [_record(root, "task_a")]
            kwargs = self._one_kwargs(root, records, "task_a")
            old = os.environ.get("GENG_CODEX_TASK_REPORTER_CMD")
            os.environ["GENG_CODEX_TASK_REPORTER_CMD"] = _fake_task_reporter(root, verdict="revise", target="writer")
            try:
                item = run_codex_task_reporter_workflow(**kwargs)
            finally:
                if old is None: os.environ.pop("GENG_CODEX_TASK_REPORTER_CMD", None)
                else: os.environ["GENG_CODEX_TASK_REPORTER_CMD"] = old
            self.assertTrue(item["ok"], item)
            self.assertFalse(item["accepted"])
            self.assertEqual(item["revision_target"], "writer")
            document = task_verifications_document([item])
            self.assertFalse(document["all_accepted"])
            self.assertEqual(document["tasks"][0]["verdict"], "revise")

    def test_misplaced_assets_are_advisory_and_not_copied(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            records = [_record(root, "task_a")]
            kwargs = self._one_kwargs(root, records, "task_a")
            old = os.environ.get("GENG_CODEX_TASK_REPORTER_CMD")
            os.environ["GENG_CODEX_TASK_REPORTER_CMD"] = _fake_task_reporter(root, misplaced_assets=True)
            try:
                item = run_codex_task_reporter_workflow(**kwargs)
            finally:
                if old is None: os.environ.pop("GENG_CODEX_TASK_REPORTER_CMD", None)
                else: os.environ["GENG_CODEX_TASK_REPORTER_CMD"] = old
            self.assertTrue(item["ok"])
            self.assertTrue(item["accepted"])
            self.assertTrue(any("assigned task asset directory" in issue for issue in item["asset_issues"]))
            self.assertTrue(any("assigned task asset directory" in issue for issue in item["validation_warnings"]))
            self.assertFalse((root / "case" / "report_assets" / "task_a").exists())

    def test_scientifically_accepted_missing_crop_remains_accepted_with_warning(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            records = [_record(root, "task_a")]
            kwargs = self._one_kwargs(root, records, "task_a")
            old = os.environ.get("GENG_CODEX_TASK_REPORTER_CMD")
            os.environ["GENG_CODEX_TASK_REPORTER_CMD"] = _fake_task_reporter(root, omit_paper_asset=True)
            try:
                item = run_codex_task_reporter_workflow(**kwargs)
            finally:
                if old is None: os.environ.pop("GENG_CODEX_TASK_REPORTER_CMD", None)
                else: os.environ["GENG_CODEX_TASK_REPORTER_CMD"] = old
            self.assertTrue(item["ok"])
            self.assertTrue(item["scientific_accepted"])
            self.assertTrue(item["accepted"])
            self.assertEqual(item["revision_target"], "none")
            self.assertEqual(item["crop_status"], "unresolved")
            self.assertTrue(any("finalized paper target" in issue for issue in item["asset_issues"]))
            self.assertTrue(any("finalized paper target" in issue for issue in item["validation_warnings"]))

if __name__ == "__main__":
    unittest.main()
