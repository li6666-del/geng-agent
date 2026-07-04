from __future__ import annotations

import re
import sys
import tomllib
import unittest
from pathlib import Path

from geng_agent.preflight import (
    CRITICAL_REPRO_PACKAGES,
    REQUIRED_PYTHON,
    EnvironmentReport,
    PackageStatus,
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
                "pandas", "sympy", "numba", "torch", "scikit-commpy", "galois",
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
