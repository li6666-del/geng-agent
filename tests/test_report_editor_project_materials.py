import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from geng_agent.agentic_report_editor import _editor_input_hash, _report_materials


class ReportEditorProjectMaterialsTests(unittest.TestCase):
    def make_project(self, root):
        project = root / "repro_project"
        (project / "src").mkdir(parents=True)
        (project / "outputs" / "T1").mkdir(parents=True)
        (project / "README.md").write_text("# Run\npython run_experiment.py config.json\n", encoding="utf-8")
        (project / "src" / "method.py").write_text("SOURCE_CONTENT_NOT_EDITOR_INPUT", encoding="utf-8")
        (project / "outputs" / "T1" / "results.csv").write_text("RAW_DATA_NOT_EDITOR_INPUT", encoding="utf-8")
        for name in ("run_experiment.py", "config.json", "requirements.repro.txt"):
            (project / name).write_text("present", encoding="utf-8")
        self.write_json(project / "reproducibility_manifest.json", {
            "full_command": ["python", "run_experiment.py", "config.json"],
            "smoke_command": ["python", "run_experiment.py", "config_smoke.json"],
            "execution_evidence": "execution_evidence.json",
        })
        self.write_json(project / "installation.json", {
            "install_file": "requirements.repro.txt", "python": {"python_full_version": "3.12.14"},
            "accelerator_sources": [], "observed_execution_version_mismatches": [],
        })
        self.write_json(project / "source_inventory.json", {"inventory_sha256": "recorded", "files": [
            {"path": "src/method.py"}, {"path": "outputs/T1/results.csv"},
        ]})
        return project

    @staticmethod
    def write_json(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    @staticmethod
    def materials(output, audit=None):
        return _report_materials(paper={}, runtime_result={"delivery_status": "complete"},
                                 task_records=[], output_dir=output, audit_dir=audit)

    @staticmethod
    def input_hash(output, materials):
        return _editor_input_hash(output_dir=output, paper={}, task_packets=[],
                                  runtime_result={}, report_materials=materials)

    def test_real_project_instructions_files_and_portability_reach_editor(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            project = self.make_project(root)
            audit = root / "separate_audit"
            self.write_json(audit / "03c_project_portability.json", {
                "portable": True, "smoke": {"requested": False, "ran": False},
                "execution_evidence": {"tasks_with_host_receipts": 1, "tasks_with_missing_bytes": []},
                "observations": [{"code": "retained_observation"}], "issues": [], "warnings": [],
            })
            self.write_json(root / "audit" / "03c_project_portability_final.json", {"portable": False})
            material = self.materials(root, audit)
            details = material["technical_details"]
            delivery = details["project_delivery"]
            self.assertEqual(delivery["readme"]["text"], (project / "README.md").read_bytes().decode("utf-8"))
            self.assertEqual(delivery["reproducibility_manifest"]["full_command"],
                             ["python", "run_experiment.py", "config.json"])
            self.assertTrue(delivery["project_present"])
            self.assertEqual(delivery["inventory"]["groups"]["src"]["present"], 1)
            self.assertEqual(delivery["inventory"]["groups"]["outputs"]["present"], 1)
            self.assertTrue(details["portability"]["portable"])
            self.assertFalse(details["portability"]["smoke"]["ran"])
            self.assertEqual(details["portability"]["observations"][0]["code"], "retained_observation")
            self.assertEqual(details["installation"]["observed_execution_version_mismatches"], [])
            self.assertIn("no separate environment reconstruction", details["delivery_policy"])
            serialized = json.dumps(material)
            self.assertNotIn("SOURCE_CONTENT_NOT_EDITOR_INPUT", serialized)
            self.assertNotIn("RAW_DATA_NOT_EDITOR_INPUT", serialized)

    def test_engineering_changes_invalidate_editor_identity_without_scientific_input_changes(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            project = self.make_project(root)
            first = self.input_hash(root, self.materials(root))
            self.assertEqual(first, self.input_hash(root, self.materials(root)))
            readme = project / "README.md"
            stat = readme.stat()
            readme.write_text(readme.read_text(encoding="utf-8").replace("# Run", "# RUN"), encoding="utf-8")
            os.utime(readme, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            changed_readme = self.input_hash(root, self.materials(root))
            self.assertNotEqual(first, changed_readme)
            manifest = project / "reproducibility_manifest.json"
            value = json.loads(manifest.read_text(encoding="utf-8"))
            value["full_command"][-1] = "full_config.json"
            self.write_json(manifest, value)
            changed_command = self.input_hash(root, self.materials(root))
            self.assertNotEqual(changed_readme, changed_command)
            (project / "src" / "method.py").unlink()
            material = self.materials(root)
            self.assertEqual(material["technical_details"]["project_delivery"]["inventory"]["unavailable_count"], 1)
            changed_presence = self.input_hash(root, material)
            self.assertNotEqual(changed_command, changed_presence)
            self.write_json(root / "audit" / "03c_project_portability.json", {"portable": True, "smoke": {"ran": False}})
            self.assertNotEqual(changed_presence, self.input_hash(root, self.materials(root)))

    def test_missing_material_is_unknown_and_unsafe_inventory_cannot_escape_project(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            material = self.materials(root)["technical_details"]
            self.assertFalse(material["project_delivery"]["project_present"])
            self.assertEqual(material["project_delivery"]["readme"]["status"], "missing")
            self.assertEqual(material["project_delivery"]["reproducibility_manifest"], {})
            self.assertEqual(material["portability"], {})
            project = self.make_project(root)
            (root / "outside.txt").write_text("outside", encoding="utf-8")
            self.write_json(project / "source_inventory.json", {"files": [{"path": "../outside.txt"}]})
            inventory = self.materials(root)["technical_details"]["project_delivery"]["inventory"]
            self.assertEqual(inventory["unavailable_files"], [{"path": "../outside.txt", "status": "unsafe_path"}])

    def test_readme_truncation_is_explicit_and_tail_changes_invalidate_identity(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            project = self.make_project(root)
            readme = project / "README.md"
            readme.write_text("x" * 40000, encoding="utf-8")
            first = self.materials(root)
            self.assertTrue(first["technical_details"]["project_delivery"]["readme"]["truncated"])
            self.assertEqual(len(first["technical_details"]["project_delivery"]["readme"]["text"]), 32000)
            readme.write_text("x" * 39999 + "y", encoding="utf-8")
            self.assertNotEqual(self.input_hash(root, first), self.input_hash(root, self.materials(root)))
