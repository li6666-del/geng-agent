from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from geng_agent.security import FORBIDDEN_BUILTINS, static_scan_repro_project


def scan_source(source: str) -> list[dict[str, str]]:
    with TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        (root / "run_experiment.py").write_text(source, encoding="utf-8")
        return static_scan_repro_project(root)


def messages(issues: list[dict[str, str]]) -> list[str]:
    return [issue["message"] for issue in issues]


class StaticScanDynamicBuiltinTests(unittest.TestCase):
    def test_flags_every_forbidden_dynamic_builtin(self) -> None:
        for name in FORBIDDEN_BUILTINS:
            with self.subTest(builtin=name):
                issues = scan_source(f"x = {name}()\n")
                self.assertIn(f"forbidden dynamic builtin: {name}", messages(issues))

    def test_flags_getattr_indirection_bypass(self) -> None:
        issues = scan_source("import os\ngetattr(os, 'sys' + 'tem')('echo hi')\n")
        self.assertIn("forbidden dynamic builtin: getattr", messages(issues))

    def test_flags_dunder_import_bypass(self) -> None:
        issues = scan_source("__import__('sock' + 'et')\n")
        self.assertIn("forbidden dynamic builtin: __import__", messages(issues))

    def test_flags_eval_of_string(self) -> None:
        issues = scan_source("eval(\"__import__('os').system('echo hi')\")\n")
        self.assertIn("forbidden dynamic builtin: eval", messages(issues))

    def test_flags_importlib_import(self) -> None:
        issues = scan_source("import importlib\nimportlib.import_module('socket')\n")
        self.assertIn("forbidden import: importlib", messages(issues))

    def test_records_file_and_line(self) -> None:
        issues = scan_source("x = 1\ny = eval('2')\n")
        flagged = [issue for issue in issues if issue["message"] == "forbidden dynamic builtin: eval"]
        self.assertEqual(len(flagged), 1)
        self.assertEqual(flagged[0]["file"], "run_experiment.py")
        self.assertEqual(flagged[0]["line"], "2")

    def test_does_not_flag_dotted_or_legitimate_calls(self) -> None:
        # re.compile is an attribute call, not the bare compile builtin; numerical
        # code and relative-path file I/O must stay clean (no false positives).
        source = (
            "import re\n"
            "import numpy as np\n"
            "pattern = re.compile('ab')\n"
            "arr = np.array([1, 2, 3])\n"
            "with open('outputs/results.csv', 'w', encoding='utf-8') as fh:\n"
            "    fh.write('x\\n')\n"
        )
        self.assertEqual(scan_source(source), [])


if __name__ == "__main__":
    unittest.main()
