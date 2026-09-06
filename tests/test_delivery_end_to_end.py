"""Small cross-stage experiment: only the Codex text generator is substituted."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys
from tests.test_execution_sandbox import native_sandbox_temporary_directory as TemporaryDirectory
import textwrap
import unittest
from unittest.mock import patch

from geng_agent.codex_runner import run_codex_subprocess
from geng_agent.execution_receipts import ExecutionBroker, find_host_execution
from geng_agent.agentic_task_reporters import run_codex_task_reporter_workflow
from geng_agent.agentic_report_editor import run_codex_report_editor_workflow
from geng_agent.task_writer_packaging import _merge_task_writer_deliveries, _freeze_repro_project_package
from geng_agent.codex_cost import summarize_codex_usage


GENERATOR = r'''
import csv,json,math,os,pathlib,subprocess,sys
if '--help' in sys.argv:
    print('--ephemeral --json')
    raise SystemExit(0)
root=pathlib.Path(sys.argv[sys.argv.index('--cd')+1])
prompt=sys.stdin.read()
if 'isolated scientific task reporter' in prompt:
    # Independent deterministic reviewer reads actual evidence; it does not
    # consume a canned Writer verdict or rerun the submitted implementation.
    rows=list(csv.DictReader((root/'inputs/writer_output/outputs/results.csv').open()))
    values=[float(row['capacity']) for row in rows]
    source=(root/'inputs/writer_output/source/tasks/sample.py').read_text()
    supported=values == [1.0,2.0] and 'math.log2' in source
    note={'task_id':'sample','run_valid':True,'core_conclusions':[{'claim_id':'capacity_trend',
      'status':'supported' if supported else 'unsupported','local_observation':str(values),
      'evidence_files':['inputs/writer_output/outputs/results.csv','inputs/writer_output/source/tasks/sample.py']}],
      'key_numeric_comparisons':[{'target_id':'capacity_3','local_magnitude':values[-1]}],
      'comparison_summary':'Independent CSV and method inspection','evidence_files':['inputs/writer_output/outputs/results.csv']}
    (root/'task_verification_result.json').write_text(json.dumps(note))
elif 'final report editor' in prompt:
    for name in ('review.md','reproduction_report.md','result_review.md'):
        (root/name).write_text('# Explanation\nThe evidence is supplied separately.\n')
else:
    (root/'tasks/sample.py').write_text("import csv,json,math\nfrom pathlib import Path\ndef main(config):\n    settings=json.loads(Path(config).read_text())\n    out=Path('outputs/sample')\n    out.mkdir(parents=True,exist_ok=True)\n    with (out/'results.csv').open('w',newline='') as f:\n        w=csv.writer(f);w.writerow(['snr','capacity'])\n        for snr in settings.get('snrs',[1,3]): w.writerow([snr,math.log2(1+snr)])\n")
    completed=subprocess.run([sys.executable,str(root/'run_task.py'),'--task','sample','--mode','full'],cwd=root)
    if completed.returncode: raise SystemExit(completed.returncode)
    (root/'task_agent_result.json').write_text(json.dumps({'task_id':'sample','status':'ready_for_review','summary':'generated and executed'}))
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':10,'output_tokens':3}}))
'''


class DeliveryEndToEndTests(unittest.TestCase):
    def test_codegen_broker_reporter_package_and_clean_runtime(self):
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            case = base / "case"
            sandbox = case / "audit" / "03c_task_writer_sandboxes" / "sample"
            (sandbox / "tasks").mkdir(parents=True)
            (sandbox / "tasks" / "__init__.py").write_text("", encoding="utf-8")
            manifest = {"version": 1, "tasks": [{"task_id": "sample", "module": "sample",
                "script": "tasks/sample.py", "output_subdir": "sample", "config_full": "config.json", "config_smoke": "config_smoke.json"}]}
            (sandbox / "tasks_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            (sandbox / "config.json").write_text('{"run_profile":"full","snrs":[1,3]}', encoding="utf-8")
            (sandbox / "config_smoke.json").write_text('{"run_profile":"smoke","smoke":true}', encoding="utf-8")
            (sandbox / "requirements.txt").write_text("# stdlib only\n", encoding="utf-8")
            shutil.copyfile(Path(__file__).parents[1] / "geng_agent" / "execution_client.py", sandbox / "run_task.py")
            generator = base / "generator.py"
            generator.write_text(textwrap.dedent(GENERATOR), encoding="utf-8")
            command = f'"{sys.executable}" "{generator}"'
            task_audit = case / "audit" / "writer_observer"
            with ExecutionBroker(sandbox, task_audit, Path(sys.executable)) as broker:
                status = run_codex_subprocess(role="task_writer", work_dir=sandbox, prompt="Generate the requested scalar channel experiment",
                    audit_dir=task_audit, label="writer", sandbox="workspace-write", command_override=command,
                    extra_env={"GENG_EXECUTION_BROKER": broker.session_id})
            self.assertTrue(status["ok"], status)
            host = find_host_execution(sandbox, task_audit, "sample")
            self.assertTrue(host["passed"], host)
            record = {"task_id": "sample", "module": "sample", "sandbox": str(sandbox), "output_subdir": "sample",
                      "task_writer_status": "ready_for_review", "writer_completed": True, "host_execution": host,
                      "result_json": json.loads((sandbox / "task_agent_result.json").read_text())}
            task = {"task_id": "sample", "figure_or_claim": "Capacity increases with SNR", "required_facts": [], "assumptions": [],
                    "scientific_acceptance": {"core_conclusions": [{"claim_id": "capacity_trend", "statement": "Capacity increases"}],
                    "key_numeric_targets": [{"target_id": "capacity_3", "paper_magnitude": 2.0}], "information_gaps": []}}
            paper = base / "paper.md"
            paper.write_text("Capacity is log2(1+SNR) bits/use and increases with SNR.", encoding="utf-8")
            with patch.dict(os.environ, {"GENG_CODEX_TASK_REPORTER_CMD": command, "GENG_CODEX_REPORT_EDITOR_CMD": command}):
                reporter = run_codex_task_reporter_workflow(index=1, task=task, task_record=record, paper={}, paper_path=paper,
                    facts={"engineering_facts": []}, experiment_index={}, paper_thesis=None, paper_images=[],
                    output_dir=case, audit_dir=case / "audit", resume=False)
                self.assertEqual(reporter["scientific_outcome"], "reproduced", reporter)
                editor = run_codex_report_editor_workflow(paper={}, facts={}, tasks={"repro_tasks": [task]}, paper_thesis=None,
                    runtime_result={}, risk_report={}, task_records=[record], task_verifications=[reporter["task_verification"]],
                    output_dir=case, audit_dir=case / "audit", resume=False)
            self.assertTrue(editor["ok"], editor)
            self.assertIn("已复现", (case / "review.md").read_text(encoding="utf-8"))
            project = case / "repro_project"
            expected = _merge_task_writer_deliveries(repro_project_dir=project, task_manifest=manifest,
                expected_paths=set(), task_records=[record])
            (project / "tasks" / "__init__.py").write_text("", encoding="utf-8")
            (project / "tasks_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            (project / "requirements.txt").write_text("# stdlib only\n", encoding="utf-8")
            (project / "run_experiment.py").write_text("import sys\nfrom tasks.sample import main\nmain(sys.argv[1])\n", encoding="utf-8")
            expected.update({"tasks/__init__.py", "tasks_manifest.json", "run_experiment.py"})
            _, portability = _freeze_repro_project_package(repro_project_dir=project, output_dir=case,
                audit_path=case / "audit" / "portability.json", task_manifest=manifest, expected_paths=expected,
                analysis_snapshot_hash="fixture", foundation_snapshot_hash="", environment_hash="", run_smoke=True,
                python_executable=Path(sys.executable))
            self.assertTrue(portability["clean_environment"]["verified"], portability)
            self.assertTrue((project / "outputs" / "sample" / "execution_receipt.json").is_file())
            evidence = json.loads((project / "execution_evidence.json").read_text())
            self.assertTrue(evidence["tasks"][0]["all_bytes_available"], evidence)
            config_map = next(item for item in evidence["tasks"][0]["files"] if item["original_path"] == "config.json")
            self.assertEqual(config_map["packaged_path"], "configs/sample_config.json")
            self.assertFalse((project / ".geng_execution").exists())
            self.assertFalse((project / ".geng_runtime").exists())
            self.assertEqual(summarize_codex_usage(case / "audit")["llm_calls"], 3)


if __name__ == "__main__":
    unittest.main()
