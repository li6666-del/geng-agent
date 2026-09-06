import json
import os
import sys
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from geng_agent.agentic_task_reporters import (
    _accepted_asset_issues,
    _build_task_reporter_brief,
    _task_only_facts,
    _task_record_run_valid_hint,
    _task_reporter_input_hash,
    run_codex_task_reporter_workflow,
    task_verifications_document,
)
from geng_agent.task_reporter_context import TASK_REPORTER_PROMPT_VERSION
from geng_agent.task_writer_dispatch import _refresh_cached_task_reporters
from geng_agent.report_editor_assets import (
    _build_task_packets,
    _sanitize_task_packet_assets,
)
from geng_agent.task_reporter_validation import _materialize_task_assets
from geng_agent.verification_result import (
    normalize_task_verification,
    task_verification_issues,
    verification_result_issues,
    writer_revision_allowed,
)


def _task() -> dict:
    return {
        "task_id": "task_a",
        "figure_or_claim": "Fig. 1 ordering and scale",
        "required_facts": [],
        "assumptions": [],
        "scientific_acceptance": {
            "core_conclusions": [{
                "claim_id": "claim_order",
                "kind": "ordering",
                "statement": "method A remains above method B",
                "paper_evidence": ["paper_evidence/index.json"],
            }],
            "key_numeric_targets": [{
                "target_id": "target_scale",
                "name": "reported scale",
                "paper_magnitude": 1.0,
                "evidence_quality": "explicit",
                "paper_evidence": ["paper_evidence/index.json"],
            }],
            "information_gaps": [],
        },
    }


def _record(root: Path) -> dict:
    sandbox = root / "writer_sandbox"
    output = sandbox / "outputs" / "task_a"
    output.mkdir(parents=True)
    (output / "results.csv").write_text("x,a,b\n0,5,1\n", encoding="utf-8")
    tasks_dir = sandbox / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "__init__.py").write_text("", encoding="utf-8")
    (tasks_dir / "task_a.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tasks_dir / "kernel.cu").write_text("// readable CUDA source\n", encoding="utf-8")
    src_dir = sandbox / "src"
    src_dir.mkdir()
    (src_dir / "_io.py").write_text("def finish(): return 0\n", encoding="utf-8")
    (sandbox / "config.json").write_text('{"run_profile": "full"}\n', encoding="utf-8")
    (sandbox / "config_smoke.json").write_text('{"run_profile": "smoke"}\n', encoding="utf-8")
    (sandbox / "requirements.txt").write_text("numpy\n", encoding="utf-8")
    return {
        "task_id": "task_a",
        "sandbox": str(sandbox),
        "output_subdir": "task_a",
        "writer_completed": True,
        "task_writer_status": "ready_for_review",
        "delivery_blockers": [],
        "result_json": {
            "task_id": "task_a",
            "status": "ready_for_review",
            "summary": "full run completed",
            "execution_summary": {"full_run_count": 1, "last_returncode": 0},
        },
        "execution_summary": {"full_run_count": 1, "last_returncode": 0},
    }


def _supported_raw(*, local_magnitude: float = 5.0) -> dict:
    return {
        "schema_version": "2.0",
        "task_id": "task_a",
        "run_valid": True,
        "core_conclusions": [{
            "claim_id": "claim_order",
            "status": "supported",
            "local_observation": "A remains above B in the full result",
            "evidence_files": ["inputs/writer_output/outputs/results.csv"],
        }],
        "key_numeric_comparisons": [{
            "target_id": "target_scale",
            "local_magnitude": local_magnitude,
            "symmetric_ratio": 1.0,
        }],
        "comparison_summary": "the core ordering is supported",
        "confidence": "high",
    }


def _rerun_raw(*, paper_evidence: str, claim_id: str = "claim_order") -> dict:
    return {
        "schema_version": "2.0",
        "task_id": "task_a",
        "run_valid": True,
        "core_conclusions": [{
            "claim_id": claim_id,
            "status": "unsupported",
            "local_observation": "the full result reverses the paper ordering",
            "evidence_files": ["inputs/writer_output/outputs/results.csv"],
        }],
        "rerun_evidence": {
            "rerun_reason": "core_conclusion_failed",
            "contract_item_ids": [claim_id],
            "paper_evidence_files": [paper_evidence],
            "causal_change": "correct the paper-defined normalization",
            "change_targets": ["tasks/task_a.py:normalization"],
            "predicted_effect": "restore the paper ordering",
        },
        "comparison_summary": "the core ordering is currently unsupported",
    }


def _fake_reporter_command(
    root: Path,
    payload: dict,
    *,
    asset_files: dict[str, bytes] | None = None,
) -> str:
    script = root / "fake_task_reporter.py"
    encoded = json.dumps(payload, ensure_ascii=False)
    encoded_assets = json.dumps(
        {path: value.hex() for path, value in (asset_files or {}).items()},
        ensure_ascii=False,
    )
    script.write_text(textwrap.dedent(f"""
        import json
        import sys
        from pathlib import Path

        if sys.argv[1:] == ["exec", "--help"]:
            print("--ephemeral")
            raise SystemExit(0)

        result = json.loads({encoded!r})
        assets = json.loads({encoded_assets!r})
        for relative, payload_hex in assets.items():
            target = Path(relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(bytes.fromhex(payload_hex))
        Path("task_verification_result.json").write_text(
            json.dumps(result, ensure_ascii=False),
            encoding="utf-8",
        )
    """), encoding="utf-8")
    return f'"{sys.executable}" "{script}"'


def _run_reporter(
    root: Path,
    payload: dict,
    *,
    resume: bool = False,
    record: dict | None = None,
    reporter_assets: dict[str, bytes] | None = None,
    assigned_task: dict | None = None,
    assigned_experiment_index: dict | None = None,
) -> dict:
    paper = root / "paper.md"
    if not paper.exists():
        paper.write_text("# Paper\n\nFig. 1 reports A above B at scale 1.", encoding="utf-8")
    task_record = record or _record(root)
    command = _fake_reporter_command(
        root,
        payload,
        asset_files=reporter_assets,
    )
    with patch.dict(os.environ, {"GENG_CODEX_TASK_REPORTER_CMD": command}):
        return run_codex_task_reporter_workflow(
            index=1,
            task=assigned_task or _task(),
            task_record=task_record,
            paper={"format": "markdown", "chunks": []},
            paper_path=paper,
            facts={"paper_domain": "test", "paper_repro_type": "figure", "engineering_facts": [], "missing_information": []},
            experiment_index=(
                assigned_experiment_index
                or {"experiments": [{"task_id": "task_a", "source_pages": []}]}
            ),
            paper_thesis=None,
            paper_images=[],
            output_dir=root / "case",
            audit_dir=root / "case" / "audit",
            resume=resume,
            round_no=1,
        )


class IsolatedTaskReporterTests(unittest.TestCase):
    def test_prompt_uses_small_advisory_contract_and_host_owned_materiality(self) -> None:
        prompt = _build_task_reporter_brief(
            task_id="task_a",
            report_asset_dir="report_assets/task_a",
            include_all_paper_pages=False,
        )

        self.assertIn("navigation aid", prompt)
        self.assertIn("never reject merely for missing structure", prompt)
        self.assertIn("Do not select new key quantities", prompt)
        self.assertIn("the host owns the paper target and arithmetic", prompt)
        self.assertIn("invalid_run", prompt)
        self.assertIn("core_conclusion_failed", prompt)
        self.assertIn("key_numeric_ratio_ge_10", prompt)
        self.assertIn("not_reproduced", prompt)
        self.assertIn("inconclusive_missing_information", prompt)
        self.assertIn("Visual packaging is independent of the scientific outcome", prompt)
        self.assertIn("PNG/JPG/JPEG", prompt)
        self.assertIn("CSV, JSON, PDF", prompt)
        self.assertEqual(
            TASK_REPORTER_PROMPT_VERSION,
            "isolated_task_reporter_v9_lossless_scientific_observations",
        )
        self.assertNotIn('"verdict"', prompt)
        self.assertNotIn('"revision_target"', prompt)

    def test_missing_structure_becomes_terminal_inconclusive(self) -> None:
        result = normalize_task_verification(
            {"task_id": "wrong_task", "comparison_summary": "no itemized evidence"},
            "task_a",
            task=_task(),
            run_valid_hint=True,
        )

        self.assertEqual(result["task_id"], "task_a")
        self.assertEqual(result["host_action"], "complete")
        self.assertEqual(result["outcome"], "inconclusive_missing_information")
        self.assertEqual(result["core_conclusions"][0]["status"], "unassessable_missing_information")
        self.assertEqual(task_verification_issues(result, "task_a"), [])

    def test_host_recomputes_numeric_ratio_and_ignores_reporter_ratio(self) -> None:
        task = _task()
        task["scientific_acceptance"]["key_numeric_targets"][0]["paper_magnitude"] = 2.0
        raw = _supported_raw(local_magnitude=8.0)
        raw["key_numeric_comparisons"][0]["symmetric_ratio"] = 1.0

        result = normalize_task_verification(raw, "task_a", task=task, run_valid_hint=True)

        self.assertEqual(result["max_key_numeric_ratio"], 4.0)
        self.assertEqual(result["outcome"], "reproduced")
        self.assertEqual(result["host_action"], "complete")

    def test_order_of_magnitude_gap_is_terminal_without_causal_plan(self) -> None:
        result = normalize_task_verification(
            _supported_raw(local_magnitude=10.0),
            "task_a",
            task=_task(),
            run_valid_hint=True,
        )

        self.assertEqual(result["max_key_numeric_ratio"], 10.0)
        self.assertEqual(result["outcome"], "not_reproduced")
        self.assertEqual(result["host_action"], "complete")
        self.assertFalse(writer_revision_allowed(result, "task_a"))

    def test_uncontracted_reporter_numeric_gap_is_preserved(self) -> None:
        task = _task()
        raw = _supported_raw(local_magnitude=1.0)
        raw["key_numeric_comparisons"].append({
            "target_id": "reporter_discovered_scale",
            "name": "paper-discovered scale",
            "paper_magnitude": 2.0,
            "local_magnitude": 20.0,
        })

        result = normalize_task_verification(
            raw,
            "task_a",
            task=task,
            run_valid_hint=True,
        )

        self.assertEqual(len(result["key_numeric_comparisons"]), 2)
        self.assertEqual(
            result["key_numeric_comparisons"][1]["target_id"],
            "reporter_discovered_scale",
        )
        self.assertEqual(result["max_key_numeric_ratio"], 10.0)
        self.assertEqual(result["outcome"], "not_reproduced")

    def test_complete_causal_evidence_allows_one_writer_rerun(self) -> None:
        result = normalize_task_verification(
            _rerun_raw(paper_evidence="paper_evidence/index.json"),
            "task_a",
            task=_task(),
            run_valid_hint=True,
        )

        self.assertEqual(result["host_action"], "rerun_writer")
        self.assertEqual(result["rerun_reason"], "core_conclusion_failed")
        self.assertTrue(writer_revision_allowed(result, "task_a"))

    def test_reporter_discovered_failure_is_not_erased_by_unknown_designer_id(self) -> None:
        result = normalize_task_verification(
            _rerun_raw(
                paper_evidence="paper_evidence/index.json",
                claim_id="invented_claim",
            ),
            "task_a",
            task=_task(),
            run_valid_hint=True,
        )

        self.assertEqual(result["host_action"], "rerun_writer")
        self.assertEqual(result["rerun_reason"], "core_conclusion_failed")
        self.assertEqual(result["outcome"], "not_reproduced")
        self.assertTrue(writer_revision_allowed(result, "task_a"))
        self.assertEqual(result["core_conclusions"][1]["claim_id"], "invented_claim")

    def test_wrong_numeric_target_id_cannot_be_counted_as_success(self) -> None:
        raw = _supported_raw(local_magnitude=1.0)
        raw["key_numeric_comparisons"][0]["target_id"] = "invented_target"

        result = normalize_task_verification(
            raw,
            "task_a",
            task=_task(),
            run_valid_hint=True,
        )

        comparison = result["key_numeric_comparisons"][0]
        self.assertEqual(comparison["target_id"], "target_scale")
        self.assertIsNone(comparison["local_magnitude"])
        self.assertEqual(result["outcome"], "inconclusive_missing_information")
        self.assertEqual(result["host_action"], "complete")

    def test_aggregate_accepts_mixed_reportable_terminal_outcomes(self) -> None:
        reproduced = normalize_task_verification(
            _supported_raw(), "task_a", task=_task(), run_valid_hint=True
        )
        failed_task = _task()
        failed_task["task_id"] = "task_b"
        failed_task["scientific_acceptance"]["core_conclusions"][0]["claim_id"] = "claim_b"
        failed_raw = {
            "task_id": "task_b",
            "run_valid": True,
            "core_conclusions": [{
                "claim_id": "claim_b",
                "status": "unsupported",
                "local_observation": "the paper ordering was not reproduced",
            }],
        }
        not_reproduced = normalize_task_verification(
            failed_raw, "task_b", task=failed_task, run_valid_hint=True
        )

        document = task_verifications_document([
            {"task_verification": reproduced},
            {"task_verification": not_reproduced},
        ])

        self.assertTrue(document["all_terminal"])
        self.assertFalse(document["all_successful"])
        self.assertEqual(document["outcome_counts"], {"reproduced": 1, "not_reproduced": 1})
        self.assertEqual(verification_result_issues(document, ["task_a", "task_b"]), [])

    def test_visual_assets_are_optional_but_supplied_paths_are_scoped(self) -> None:
        with TemporaryDirectory() as temp:
            workspace = Path(temp)
            self.assertEqual(
                _accepted_asset_issues(
                    {"task_id": "task_a"},
                    workspace,
                    "task_a",
                    crop_result={"status": "not_applicable"},
                    require_verified_pdf_crop=True,
                ),
                [],
            )
            misplaced = workspace / "elsewhere.png"
            misplaced.write_bytes(b"png")
            issues = _accepted_asset_issues(
                {"task_id": "task_a", "local_assets": ["elsewhere.png"]},
                workspace,
                "task_a",
                crop_result={"status": "not_applicable"},
            )
            self.assertTrue(any("outside the assigned asset directory" in issue for issue in issues))

    def test_host_materialization_rejects_asset_symlinks(self) -> None:
        with TemporaryDirectory() as temp:
            workspace = Path(temp)
            output = workspace / "inputs" / "writer_output" / "outputs"
            output.mkdir(parents=True)
            target = output / "real.png"
            target.write_bytes(b"png")
            linked = output / "linked.png"
            try:
                os.symlink(target, linked)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlink creation is unavailable: {exc}")

            published, warnings = _materialize_task_assets(
                asset_candidates={
                    "local_assets": ["inputs/writer_output/outputs/linked.png"],
                    "paper_assets": [],
                },
                workspace=workspace,
                task_id="task_a",
            )

            self.assertEqual(published, {"local_assets": [], "paper_assets": []})
            self.assertTrue(any("symbolic links" in warning for warning in warnings))

    def test_writer_output_symlink_is_skipped_without_failing_reporter(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            record = _record(root)
            external = root / "outside.txt"
            external.write_text("outside", encoding="utf-8")
            linked = Path(record["sandbox"]) / "outputs" / "task_a" / "linked.txt"
            try:
                os.symlink(external, linked)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlink creation is unavailable: {exc}")

            item = _run_reporter(root, _supported_raw(), record=record)

            self.assertTrue(item["ok"], item)
            workspace = Path(item["workspace"])
            self.assertFalse((workspace / "inputs" / "writer_output" / "outputs" / "linked.txt").exists())
            report_input = json.loads(
                (workspace / "inputs" / "task_report_input.json").read_text(encoding="utf-8")
            )
            self.assertTrue(any("symbolic link" in warning for warning in report_input["input_warnings"]))

    def test_writer_output_resource_limits_only_skip_excess_files(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            record = _record(root)
            output = Path(record["sandbox"]) / "outputs" / "task_a"
            (output / "a_oversized.bin").write_bytes(b"x" * 128)
            (output / "small_a.bin").write_bytes(b"a" * 30)
            (output / "small_b.bin").write_bytes(b"b" * 30)

            with patch("geng_agent.agentic_task_reporters._WRITER_OUTPUT_MAX_FILE_BYTES", 64), patch(
                "geng_agent.agentic_task_reporters._WRITER_OUTPUT_MAX_TOTAL_BYTES", 60
            ):
                item = _run_reporter(root, _supported_raw(), record=record)

            self.assertTrue(item["ok"], item)
            workspace = Path(item["workspace"])
            copied = workspace / "inputs" / "writer_output" / "outputs"
            self.assertFalse((copied / "a_oversized.bin").exists())
            self.assertTrue((copied / "small_a.bin").is_file())
            self.assertFalse((copied / "small_b.bin").exists())
            report_input = json.loads(
                (workspace / "inputs" / "task_report_input.json").read_text(encoding="utf-8")
            )
            warnings = report_input["input_warnings"]
            self.assertTrue(any("per-file resource limit" in warning for warning in warnings))
            self.assertTrue(any("total resource limit" in warning for warning in warnings))

    def test_cache_hash_includes_writer_source_content_and_analysis_inputs(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            record = _record(root)
            paper = root / "paper.md"
            paper.write_text("Fig. 1", encoding="utf-8")
            source = Path(record["sandbox"]) / "tasks" / "task_a.py"
            stat = source.stat()
            kwargs = {
                "task": _task(),
                "task_record": record,
                "paper_path": paper,
                "facts": {"engineering_facts": []},
                "experiment_index": {"experiments": []},
                "paper_thesis": {},
                "figure_candidates": [],
            }
            first = _task_reporter_input_hash(**kwargs)
            os.utime(source, None)
            touched = _task_reporter_input_hash(**kwargs)
            source.write_text("VALUE = 2\n", encoding="utf-8")
            os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            second = _task_reporter_input_hash(**kwargs)
            kwargs["paper_thesis"] = {"central_claim": "changed"}
            third = _task_reporter_input_hash(**kwargs)

            self.assertEqual(first, touched)
            self.assertNotEqual(first, second)
            self.assertNotEqual(second, third)

    def test_workflow_terminal_success_is_cacheable_without_images(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            record = _record(root)
            first = _run_reporter(root, _supported_raw(), record=record)
            second = _run_reporter(root, _supported_raw(), record=record, resume=True)

            self.assertTrue(first["ok"], first)
            self.assertTrue(first["terminal"])
            self.assertTrue(first["scientific_successful"])
            self.assertEqual(first["scientific_outcome"], "reproduced")
            self.assertEqual(first["asset_issues"], [])
            self.assertTrue(second["cached"])
            workspace = Path(first["workspace"])
            self.assertTrue((workspace / "inputs" / "writer_output" / "outputs" / "results.csv").is_file())
            self.assertTrue((workspace / "inputs" / "writer_output" / "source" / "tasks" / "task_a.py").is_file())
            self.assertTrue((workspace / "inputs" / "writer_output" / "source" / "tasks" / "kernel.cu").is_file())
            self.assertFalse((workspace / "inputs" / "writer_output" / "source" / "outputs").exists())

    def test_not_reproduced_terminal_publishes_safe_local_and_paper_images(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            record = _record(root)
            writer_image = Path(record["sandbox"]) / "outputs" / "task_a" / "local_plot.png"
            writer_image.write_bytes(b"local-png")
            raw = _supported_raw()
            raw["core_conclusions"][0]["status"] = "unsupported"
            raw["core_conclusions"][0]["local_observation"] = "A falls below B"
            raw["local_assets"] = ["inputs/writer_output/outputs/local_plot.png"]
            raw["paper_assets"] = ["report_assets/task_a/paper_plot.jpg"]

            item = _run_reporter(
                root,
                raw,
                record=record,
                reporter_assets={"report_assets/task_a/paper_plot.jpg": b"paper-jpg"},
            )

            self.assertTrue(item["ok"], item)
            self.assertTrue(item["terminal"])
            self.assertEqual(item["scientific_outcome"], "not_reproduced")
            self.assertEqual(
                item["task_verification"]["local_assets"],
                ["report_assets/task_a/local_plot.png"],
            )
            self.assertEqual(
                item["task_verification"]["paper_assets"],
                ["report_assets/task_a/paper_plot.jpg"],
            )
            self.assertTrue((root / "case" / "report_assets" / "task_a" / "local_plot.png").is_file())
            self.assertTrue((root / "case" / "report_assets" / "task_a" / "paper_plot.jpg").is_file())
            self.assertEqual(len(item["asset_manifest"]), 2)
            packets = _build_task_packets(
                facts={"engineering_facts": []},
                tasks={"repro_tasks": [_task()]},
                task_records=[record],
                task_verifications=[item["task_verification"]],
            )
            warnings = _sanitize_task_packet_assets(
                packets,
                root / "case" / "report_assets",
            )
            self.assertEqual(warnings, [])
            self.assertEqual(
                packets[0]["local_assets"],
                ["report_assets/task_a/local_plot.png"],
            )
            self.assertEqual(
                packets[0]["paper_assets"],
                ["report_assets/task_a/paper_plot.jpg"],
            )

    def test_only_materialized_images_reach_normalized_asset_lists(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            record = _record(root)
            writer_output = Path(record["sandbox"]) / "outputs" / "task_a"
            (writer_output / "local_plot.png").write_bytes(b"local-png")
            raw = _supported_raw()
            raw["local_assets"] = [
                "inputs/writer_output/outputs/local_plot.png",
                "inputs/writer_output/outputs/results.csv",
            ]
            raw["paper_assets"] = ["paper_evidence/source/paper.md"]

            item = _run_reporter(root, raw, record=record)

            self.assertTrue(item["ok"], item)
            self.assertEqual(
                item["task_verification"]["local_assets"],
                ["report_assets/task_a/local_plot.png"],
            )
            self.assertEqual(item["task_verification"]["paper_assets"], [])
            self.assertTrue(any("only ordinary PNG/JPG/JPEG" in issue for issue in item["asset_issues"]))
            published = root / "case" / "report_assets" / "task_a"
            self.assertTrue((published / "local_plot.png").is_file())
            self.assertFalse((published / "results.csv").exists())
            self.assertFalse((published / "paper.md").exists())
            self.assertEqual(
                [entry["path"] for entry in item["asset_manifest"]],
                ["report_assets/task_a/local_plot.png"],
            )

    def test_declared_asset_cache_rejects_tampering_and_missing_files(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            record = _record(root)
            writer_image = Path(record["sandbox"]) / "outputs" / "task_a" / "local_plot.png"
            writer_image.write_bytes(b"local-png")
            raw = _supported_raw()
            raw["local_assets"] = ["inputs/writer_output/outputs/local_plot.png"]

            first = _run_reporter(root, raw, record=record)
            cached = _run_reporter(root, raw, record=record, resume=True)
            published = root / "case" / "report_assets" / "task_a" / "local_plot.png"
            published.write_bytes(b"tampered!")
            repaired_tamper = _run_reporter(root, raw, record=record, resume=True)
            published.unlink()
            repaired_missing = _run_reporter(root, raw, record=record, resume=True)

            self.assertTrue(first["ok"], first)
            self.assertTrue(cached["cached"], cached)
            self.assertFalse(repaired_tamper["cached"], repaired_tamper)
            self.assertFalse(repaired_missing["cached"], repaired_missing)
            self.assertTrue(published.is_file())

    def test_cached_writer_revalidates_reporter_assets_and_prompt_hash_independently(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            record = _record(root)
            writer_image = Path(record["sandbox"]) / "outputs" / "task_a" / "local_plot.png"
            writer_image.write_bytes(b"local-png")
            raw = _supported_raw()
            raw["local_assets"] = ["inputs/writer_output/outputs/local_plot.png"]
            first = _run_reporter(root, raw, record=record)
            record["writer_session_count"] = 1
            record["task_verification"] = first["task_verification"]
            task_pairs = [
                (
                    _task(),
                    {
                        "task_id": "task_a",
                        "module": "task_a",
                        "script": "tasks/task_a.py",
                        "output_subdir": "task_a",
                    },
                )
            ]

            def refresh() -> tuple[dict, dict]:
                refreshed, _replay, revisions, audit = _refresh_cached_task_reporters(
                    task_pairs=task_pairs,
                    cached_records=[record],
                    experiment_index={"experiments": []},
                    task_review_callback=lambda index, task, cached_record, round_no: _run_reporter(
                        root,
                        raw,
                        resume=True,
                        record=cached_record,
                    ),
                )
                self.assertEqual(revisions, {})
                self.assertEqual(audit["writer_sessions_launched"], 0)
                return refreshed[0]["task_reporter"], audit

            valid_cache, valid_audit = refresh()
            self.assertTrue(valid_cache["cached"], valid_cache)
            self.assertEqual(valid_audit["actions"][0]["reporter_cached"], True)

            old_workspace = Path(first["workspace"])
            marker = old_workspace / "preserve_old_reporter_round.txt"
            marker.write_text("old audit evidence", encoding="utf-8")
            published = root / "case" / "report_assets" / "task_a" / "local_plot.png"
            published.write_bytes(b"tampered")
            asset_miss, asset_audit = refresh()
            self.assertFalse(asset_miss["cached"], asset_miss)
            self.assertEqual(asset_miss["round_no"], 2)
            self.assertTrue(marker.is_file())
            self.assertEqual(asset_audit["actions"][0]["reporter_cached"], False)
            self.assertEqual(published.read_bytes(), b"local-png")

            with patch(
                "geng_agent.task_reporter_context.TASK_REPORTER_PROMPT_VERSION",
                TASK_REPORTER_PROMPT_VERSION + "_test_next",
            ):
                prompt_miss, _ = refresh()
                prompt_hit, _ = refresh()

            self.assertFalse(prompt_miss["cached"], prompt_miss)
            self.assertEqual(prompt_miss["round_no"], 3)
            self.assertTrue(prompt_hit["cached"], prompt_hit)
            self.assertEqual(prompt_hit["round_no"], 3)
            self.assertTrue(marker.is_file())

    def test_cached_writer_refresh_preserves_enriched_task_reporter_cache_hit(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            record = _record(root)
            experiment_index = {
                "experiments": [
                    {
                        "task_id": "task_a",
                        "experiment_id": "experiment_alpha",
                        "source_pages": [],
                    }
                ]
            }
            enriched_task = {**_task(), "experiment_id": "experiment_alpha"}
            first = _run_reporter(
                root,
                _supported_raw(),
                record=record,
                assigned_task=enriched_task,
                assigned_experiment_index=experiment_index,
            )
            record["writer_session_count"] = 1
            record["task_verification"] = first["task_verification"]
            callback_tasks: list[dict] = []

            def callback(index, assigned_task, cached_record, round_no):
                callback_tasks.append(assigned_task)
                return _run_reporter(
                    root,
                    _supported_raw(),
                    resume=True,
                    record=cached_record,
                    assigned_task=assigned_task,
                    assigned_experiment_index=experiment_index,
                )

            refreshed, _replay, revisions, audit = _refresh_cached_task_reporters(
                task_pairs=[
                    (
                        _task(),
                        {
                            "task_id": "task_a",
                            "module": "task_a",
                            "script": "tasks/task_a.py",
                            "output_subdir": "task_a",
                        },
                    )
                ],
                cached_records=[record],
                experiment_index=experiment_index,
                task_review_callback=callback,
            )

            self.assertEqual(revisions, {})
            self.assertEqual(callback_tasks[0]["experiment_id"], "experiment_alpha")
            self.assertTrue(refreshed[0]["task_reporter"]["cached"])
            self.assertEqual(audit["actions"][0]["reporter_cached"], True)

    def test_oversized_declared_image_is_advisory_and_not_published(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            record = _record(root)
            writer_image = Path(record["sandbox"]) / "outputs" / "task_a" / "large.png"
            writer_image.write_bytes(b"123456789")
            raw = _supported_raw()
            raw["local_assets"] = ["inputs/writer_output/outputs/large.png"]

            with patch(
                "geng_agent.task_reporter_validation.REPORT_ASSET_MAX_BYTES",
                8,
            ):
                item = _run_reporter(root, raw, record=record)

            self.assertTrue(item["ok"], item)
            self.assertTrue(item["terminal"])
            self.assertEqual(item["task_verification"]["local_assets"], [])
            self.assertEqual(item["asset_manifest"], [])
            self.assertTrue(any("exceeds 8 bytes" in issue for issue in item["asset_issues"]))

    def test_optional_crop_failure_does_not_change_scientific_terminal_state(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            with patch(
                "geng_agent.agentic_task_reporters.finalize_paper_target",
                side_effect=RuntimeError("crop backend unavailable"),
            ):
                item = _run_reporter(root, _supported_raw())

            self.assertTrue(item["ok"], item)
            self.assertTrue(item["terminal"])
            self.assertEqual(item["scientific_outcome"], "reproduced")
            self.assertEqual(item["crop_status"], "unresolved")
            self.assertIn("optional paper crop failed", item["crop_result"]["issues"][0])

    def test_workflow_declines_missing_paper_evidence_without_failing(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            item = _run_reporter(
                root,
                _rerun_raw(paper_evidence="paper_evidence/missing.json"),
            )

            self.assertTrue(item["ok"], item)
            self.assertTrue(item["terminal"])
            self.assertEqual(item["task_verification"]["host_action"], "complete")
            self.assertEqual(item["task_verification"]["outcome"], "not_reproduced")
            self.assertTrue(any("missing or outside" in issue for issue in item["validation_warnings"]))

    def test_workflow_preserves_trusted_causal_rerun_request(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            item = _run_reporter(
                root,
                _rerun_raw(paper_evidence="paper_evidence/index.json"),
            )

            self.assertTrue(item["ok"], item)
            self.assertFalse(item["terminal"])
            self.assertEqual(item["task_verification"]["host_action"], "rerun_writer")
            self.assertEqual(item["task_verification"]["rerun_reason"], "core_conclusion_failed")

    def test_malformed_writer_execution_metadata_is_advisory(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            record = _record(root)
            record["execution_summary"] = {"full_run_count": "not-a-number", "last_returncode": 0}
            item = _run_reporter(root, _supported_raw(), record=record)

            self.assertTrue(item["ok"], item)
            self.assertTrue(item["terminal"])
            self.assertEqual(item["task_verification"]["run_valid"], True)
            self.assertEqual(item["task_verification"]["outcome"], "reproduced")

    def test_explicit_host_execution_overrides_writer_self_report(self) -> None:
        record = {
            "host_execution": {"passed": True, "returncode": 0},
            "execution_summary": {"full_run_count": 1, "last_returncode": 1},
        }

        self.assertIs(_task_record_run_valid_hint(record), True)

    def test_only_high_impact_missing_information_is_forwarded(self) -> None:
        facts = {
            "engineering_facts": [],
            "missing_information": [
                {"name": "core normalization", "impact": "high"},
                {"name": "plot color", "impact": "low"},
            ],
        }

        isolated = _task_only_facts(facts, _task())

        self.assertEqual(
            isolated["missing_information"],
            [{"name": "core normalization", "impact": "high"}],
        )

    def test_only_material_core_assumptions_downgrade_scientific_outcome(self) -> None:
        task = _task()
        task["assumptions"] = [{"name": "plot color", "risk": "low"}]
        low_risk = normalize_task_verification(
            _supported_raw(), "task_a", task=task, run_valid_hint=True
        )
        task["assumptions"] = [{"name": "unknown normalization", "risk": "high"}]
        high_risk = normalize_task_verification(
            _supported_raw(), "task_a", task=task, run_valid_hint=True
        )

        self.assertEqual(low_risk["outcome"], "reproduced")
        self.assertEqual(high_risk["outcome"], "reproduced_with_assumptions")


if __name__ == "__main__":
    unittest.main()
