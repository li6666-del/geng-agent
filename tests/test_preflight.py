from __future__ import annotations

import re
import sys
import tomllib
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from geng_agent.preflight import (
    CRITICAL_REPRO_PACKAGES,
    REQUIRED_PYTHON,
    EnvironmentReport,
    ExternalToolStatus,
    PackageStatus,
    _probe_nvidia_devices,
    architecture_capability_inventory,
    architecture_execution_capability_gaps,
    check_environment,
    environment_warning,
    format_report,
    remedy_command,
)
from geng_agent.security import ALLOWED_REQUIREMENTS


def _load_pyproject() -> dict:
    root = Path(__file__).resolve().parents[1]
    with open(root / "pyproject.toml", "rb") as handle:
        return tomllib.load(handle)


def _pkg_name(requirement: str) -> str:
    return re.split(r"[<>=!~\[ ]", requirement, maxsplit=1)[0].strip().lower()


def _status(package: str, *, installed: bool = True, critical: bool = True) -> PackageStatus:
    return PackageStatus(
        package=package,
        import_name=package,
        installed=installed,
        version="1.0" if installed else None,
        purpose="x",
        critical=critical,
    )


def _ready_report() -> EnvironmentReport:
    return EnvironmentReport(
        interpreter="py",
        python_version="3.13.2",
        python_required="3.11+",
        python_ok=True,
        orchestrator=[_status("pypdf")],
        repro=[_status("numpy", critical=True)],
    )


def _missing_report() -> EnvironmentReport:
    return EnvironmentReport(
        interpreter="C:/py/python.exe",
        python_version="3.13.2",
        python_required="3.11+",
        python_ok=True,
        orchestrator=[_status("pypdf", installed=True)],
        repro=[
            _status("numpy", installed=False, critical=True),
            _status("reedsolo", installed=False, critical=False),
        ],
    )


class CheckEnvironmentTests(unittest.TestCase):
    def test_smoke_reports_expected_packages(self) -> None:
        report = check_environment()
        self.assertEqual(report.python_ok, sys.version_info[:2] >= REQUIRED_PYTHON)
        orchestrator_names = {item.package for item in report.orchestrator}
        self.assertIn("pypdf", orchestrator_names)
        self.assertIn("pymupdf", orchestrator_names)
        repro_names = {item.package for item in report.repro}
        self.assertEqual(
            repro_names,
            {
                "numpy", "scipy", "matplotlib", "scikit-learn", "reedsolo",
                "brotli", "pesq", "pandas", "sympy", "numba", "torch", "scikit-commpy", "galois",
                "networkx", "h5py", "tqdm",
            },
        )

    def test_pillow_listed_under_orchestrator_not_repro(self) -> None:
        report = check_environment()
        self.assertIn("pillow", {item.package for item in report.orchestrator})
        self.assertNotIn("pillow", {item.package for item in report.repro})

    def test_critical_flags(self) -> None:
        report = check_environment()
        for item in report.repro:
            self.assertEqual(item.critical, item.package in CRITICAL_REPRO_PACKAGES)
        self.assertEqual(CRITICAL_REPRO_PACKAGES, {"numpy", "scipy", "matplotlib"})

class ReportFormattingTests(unittest.TestCase):
    def test_ready_report_is_ok_and_silent(self) -> None:
        report = _ready_report()
        self.assertTrue(report.ok)
        self.assertFalse(report.fatal)
        self.assertIsNone(environment_warning(report))
        self.assertIsNone(remedy_command(report))
        self.assertIn("环境就绪", format_report(report))

    def test_missing_critical_is_fatal_with_remedy(self) -> None:
        report = _missing_report()
        self.assertTrue(report.fatal)
        self.assertFalse(report.ok)
        self.assertEqual([item.package for item in report.missing_repro_critical], ["numpy"])
        self.assertEqual([item.package for item in report.missing_repro_optional], ["reedsolo"])

        command = remedy_command(report)
        self.assertIsNotNone(command)
        self.assertIn('pip install -e ".[repro]"', command)
        self.assertIn(command, format_report(report))

        warning = environment_warning(report)
        self.assertIsNotNone(warning)
        self.assertIn("缺关键复现库", warning)
        self.assertIn("numpy", warning)
        self.assertIn("缺可选复现库", warning)

    def test_old_python_is_fatal(self) -> None:
        report = EnvironmentReport(
            interpreter="py",
            python_version="3.9.0",
            python_required="3.11+",
            python_ok=False,
            orchestrator=[_status("pypdf")],
            repro=[_status("numpy")],
        )
        self.assertTrue(report.fatal)
        self.assertIn("低于要求", environment_warning(report))

    def test_mineru_is_reported_as_optional_and_does_not_change_readiness(self) -> None:
        report = _ready_report()
        report = EnvironmentReport(
            interpreter=report.interpreter,
            python_version=report.python_version,
            python_required=report.python_required,
            python_ok=report.python_ok,
            orchestrator=report.orchestrator,
            repro=report.repro,
            mineru=ExternalToolStatus(
                name="MinerU",
                command="missing-mineru",
                available=False,
                resolved_executable=None,
                purpose="candidate figures",
            ),
        )
        self.assertTrue(report.ok)
        rendered = format_report(report)
        self.assertIn("可选论文版面工具", rendered)
        self.assertIn("missing-mineru", rendered)


class ArchitectureCapabilityInventoryTests(unittest.TestCase):
    @patch(
        "geng_agent.preflight._probe_nvidia_devices",
        return_value={
            "nvidia_smi_available": True,
            "devices": [{"index": 0, "name": "test GPU", "memory_total_mib": 16384}],
            "probe_status": "ok",
        },
    )
    def test_inventory_separates_host_feasibility_from_scientific_evidence(
        self,
        _probe: Mock,
    ) -> None:
        report = EnvironmentReport(
            interpreter="python",
            python_version="3.13.2",
            python_required="3.11+",
            python_ok=True,
            orchestrator=[],
            repro=[
                _status("torch", installed=True, critical=False),
                _status("numpy", installed=False, critical=True),
            ],
        )

        inventory = architecture_capability_inventory(report)

        self.assertEqual(inventory["evidence_class"], "host_capability_only_not_paper_evidence")
        self.assertEqual(
            inventory["installed_reproduction_packages"],
            [{"package": "torch", "import_name": "torch", "version": "1.0"}],
        )
        self.assertEqual(inventory["unavailable_allowed_reproduction_packages"], ["numpy"])
        self.assertTrue(
            inventory["interpretation"]["missing_package_must_not_trigger_silent_scientific_downgrade"]
        )
        self.assertEqual(inventory["accelerators"]["devices"][0]["name"], "test GPU")
        runtimes = {item["runtime"]: item for item in inventory["python_runtime_registry"]}
        self.assertTrue(runtimes["pytorch"]["policy_allowed"])
        self.assertFalse(runtimes["tensorflow"]["policy_allowed"])
        self.assertEqual(runtimes["tensorflow"]["status"], "environment_extension_required")
        self.assertEqual(
            {item["runtime"] for item in inventory["external_runtime_registry"]},
            {"julia", "matlab"},
        )

    def test_execution_capability_gaps_preserve_unsupported_runtime_and_device_requirements(self) -> None:
        architecture = {
            "components": [
                {
                    "id": "learned_model",
                    "execution": {
                        "primary_framework": "tensorflow",
                        "device_policy": "accelerator_required",
                    },
                },
                {
                    "id": "local_metric",
                    "execution": {
                        "primary_framework": "project_local",
                        "device_policy": "cpu",
                    },
                },
            ]
        }
        inventory = {
            "python_runtime_registry": [
                {
                    "runtime": "tensorflow",
                    "aliases": ["tensorflow", "keras"],
                    "package": "tensorflow",
                    "import_name": "tensorflow",
                    "policy_allowed": False,
                    "installed": False,
                }
            ],
            "external_runtime_registry": [],
            "accelerators": {"devices": []},
        }

        gaps = architecture_execution_capability_gaps(architecture, inventory)

        self.assertEqual(
            {item["kind"] for item in gaps},
            {"environment_extension_required", "accelerator_unavailable"},
        )
        self.assertTrue(all(item["component_id"] == "learned_model" for item in gaps))

    def test_execution_capability_gaps_include_supporting_libraries(self) -> None:
        architecture = {
            "components": [
                {
                    "id": "learned_model",
                    "execution": {
                        "primary_framework": "pytorch",
                        "supporting_libraries": [
                            "torch",
                            "scipy",
                            "tensorflow",
                            "julia",
                            "mysterylib",
                            "standard_library",
                            "scipy",
                        ],
                        "device_policy": "cpu",
                    },
                }
            ]
        }
        inventory = {
            "python_runtime_registry": [
                {
                    "runtime": "pytorch",
                    "aliases": ["torch", "pytorch"],
                    "package": "torch",
                    "import_name": "torch",
                    "policy_allowed": True,
                    "installed": True,
                },
                {
                    "runtime": "scipy",
                    "aliases": ["scipy"],
                    "package": "scipy",
                    "import_name": "scipy",
                    "policy_allowed": True,
                    "installed": False,
                },
                {
                    "runtime": "tensorflow",
                    "aliases": ["tensorflow", "keras"],
                    "package": "tensorflow",
                    "import_name": "tensorflow",
                    "policy_allowed": False,
                    "installed": False,
                },
            ],
            "external_runtime_registry": [
                {"runtime": "julia", "available": False}
            ],
            "accelerators": {"devices": []},
        }

        gaps = architecture_execution_capability_gaps(architecture, inventory)

        self.assertEqual(
            {(item["runtime"], item["kind"]) for item in gaps},
            {
                ("scipy", "runtime_package_missing"),
                ("tensorflow", "environment_extension_required"),
                ("julia", "external_runtime_unavailable"),
                ("mysterylib", "runtime_unregistered"),
            },
        )
        self.assertTrue(all(item["role"] == "supporting_library" for item in gaps))

    @patch("geng_agent.preflight.shutil.which", return_value="/usr/bin/nvidia-smi")
    @patch("geng_agent.preflight.subprocess.run")
    def test_nvidia_probe_reports_device_names_and_memory(
        self,
        run: Mock,
        _which: Mock,
    ) -> None:
        run.return_value = Mock(
            returncode=0,
            stdout="NVIDIA RTX 4090, 24564\nNVIDIA L4, 23034\n",
            stderr="",
        )

        result = _probe_nvidia_devices()

        self.assertEqual(result["probe_status"], "ok")
        self.assertEqual([item["memory_total_mib"] for item in result["devices"]], [24564, 23034])
        self.assertEqual(result["devices"][1]["name"], "NVIDIA L4")


class PyprojectSyncTests(unittest.TestCase):
    """Guard against drift between the enforced whitelist and what the documented
    install command actually installs."""

    def test_required_python_matches_pyproject(self) -> None:
        requires = _load_pyproject()["project"]["requires-python"]
        match = re.search(r"(\d+)\.(\d+)", requires)
        self.assertIsNotNone(match)
        self.assertEqual((int(match.group(1)), int(match.group(2))), REQUIRED_PYTHON)

    def test_repro_extra_covers_whitelist(self) -> None:
        data = _load_pyproject()
        core = data["project"]["dependencies"]
        repro = data["project"]["optional-dependencies"]["repro"]
        installable = {_pkg_name(item) for item in [*core, *repro]}
        canonical = {("scikit-learn" if pkg == "sklearn" else pkg) for pkg in ALLOWED_REQUIREMENTS}
        missing = canonical - installable
        self.assertFalse(missing, f"whitelist packages not installable via pyproject: {sorted(missing)}")


if __name__ == "__main__":
    unittest.main()
