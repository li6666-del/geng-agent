import ast
import csv
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from geng_agent.io_runtime import (
    IO_RUNTIME_API_DOC,
    IO_RUNTIME_PY,
    ensure_runtime_requirements,
    inject_io_runtime,
    io_slug,
)
from geng_agent.outputs import _valid_csv, _valid_png, _valid_summary_json
from geng_agent.security import static_scan_repro_project


def _load_runtime():
    namespace: dict = {}
    exec(compile(IO_RUNTIME_PY, "src/_io.py", "exec"), namespace)
    return namespace


class IoRuntimeStaticTests(unittest.TestCase):
    def test_runtime_source_compiles(self) -> None:
        ast.parse(IO_RUNTIME_PY)

    def test_runtime_passes_static_security_scan(self) -> None:
        # The injected _io.py is scanned like any other project file, so it must be
        # clean under the project's OWN forbidden import/call/builtin scanner.
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            inject_io_runtime(root)
            issues = static_scan_repro_project(root)
            self.assertEqual(issues, [], msg=f"_io.py is not scan-clean: {issues}")

    def test_api_doc_mentions_each_helper(self) -> None:
        for token in ("_io.begin", "_io.write_table", "_io.write_figure", "_io.finish"):
            self.assertIn(token, IO_RUNTIME_API_DOC)

    def test_io_slug_matches_runtime_slug(self) -> None:
        # The harness's io_slug must produce the SAME folder name as the runtime's _slug,
        # or the per-task artifact gate would look in the wrong outputs/<slug>/ directory.
        runtime_slug = _load_runtime()["_slug"]
        for value in ("reproduce_fig_7", "Fig. 6-heatmap", "  ", "a/b\\c", "4_cdf", "", "图_4"):
            self.assertEqual(io_slug(value), runtime_slug(value), msg=f"slug mismatch for {value!r}")


class IoRuntimeBehaviourTests(unittest.TestCase):
    def setUp(self) -> None:
        self._cwd = os.getcwd()
        self._tmp = TemporaryDirectory()
        os.chdir(self._tmp.name)
        self.ns = _load_runtime()

    def tearDown(self) -> None:
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def test_happy_path_produces_valid_artifacts(self) -> None:
        rng = self.ns["begin"]("reproduce_fig_7", {"seed": 7})
        self.assertTrue(hasattr(rng, "standard_normal"))
        self.ns["write_table"](
            "reproduce_fig_7",
            ["power_dbm", "scheme", "sum_rate"],
            [{"power_dbm": 20, "scheme": "ZF", "sum_rate": 3.5}],
        )
        fig, axes = plt.subplots()
        axes.plot([0, 1, 2], [1, 2, 3])
        self.ns["write_figure"]("reproduce_fig_7", "sum_rate_vs_power", fig)
        code = self.ns["finish"]("reproduce_fig_7", metrics={"rows": 1}, assumptions=[{"name": "x"}])

        self.assertEqual(code, 0)
        base = Path("outputs") / "reproduce_fig_7"
        self.assertTrue(_valid_csv(base / "results.csv"))
        self.assertTrue(_valid_png(base / "sum_rate_vs_power.png"))
        self.assertTrue(_valid_summary_json(base / "summary.json"))
        summary = json.loads((base / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["seed"], 7)

    def test_write_table_coerces_complex_array_and_nonfinite(self) -> None:
        self.ns["begin"]("t", {"seed": 1})
        self.ns["write_table"](
            "t",
            ["a", "b", "c", "d"],
            [
                {"a": complex(3.0, 0.0), "b": np.float64(2.5), "c": np.array([1.0, 3.0]), "d": float("nan")},
                {"a": np.int64(4), "b": float("inf"), "c": "ZF", "d": None},
            ],
        )
        with (Path("outputs") / "t" / "results.csv").open(encoding="utf-8") as handle:
            rows = list(csv.reader(handle))
        self.assertEqual(rows[0], ["a", "b", "c", "d"])
        # complex -> real part; numpy scalar -> number; array -> mean(=2.0); NaN/Inf/None -> blank
        self.assertEqual(rows[1], ["3.0", "2.5", "2.0", ""])
        self.assertEqual(rows[2][0], "4.0")
        self.assertEqual(rows[2][1], "")  # inf scrubbed
        self.assertEqual(rows[2][2], "ZF")  # real string passes through
        for row in rows:
            for cell in row:
                self.assertNotIn("j", cell.lower())  # no complex repr leaked
                self.assertNotIn("nan", cell.lower())
                self.assertNotIn("inf", cell.lower())

    def test_finish_scrubs_and_coerces_summary(self) -> None:
        self.ns["begin"]("t", {"seed": 3})
        # metrics is NOT a dict/list, assumptions is NOT a list, values include numpy + non-finite
        code = self.ns["finish"](
            "t",
            metrics=np.float64(9.0),
            assumptions={"name": "single"},
            extra={"vals": [np.array([1, 2]), float("nan"), complex(2.0, 0.0)]},
        )
        self.assertEqual(code, 0)
        summary = json.loads((Path("outputs") / "t" / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["metrics"], {"value": 9.0})  # scalar wrapped into dict
        self.assertEqual(summary["assumptions"], [{"name": "single"}])  # wrapped into list
        self.assertEqual(summary["vals"], [[1, 2], None, 2.0])  # array/list, NaN->null, complex->real
        # round-trips and is a *valid* summary by the harness's own validator
        self.assertTrue(_valid_summary_json(Path("outputs") / "t" / "summary.json"))

    def test_finish_ok_false_returns_nonzero_but_still_writes(self) -> None:
        self.ns["begin"]("t", {"seed": 3})
        code = self.ns["finish"]("t", metrics={}, assumptions=[], ok=False)
        self.assertEqual(code, 1)
        self.assertTrue((Path("outputs") / "t" / "summary.json").exists())

    def test_write_figure_strips_duplicate_png_extension(self) -> None:
        self.ns["begin"]("t", {"seed": 1})
        fig, axes = plt.subplots()
        axes.plot([0, 1], [1, 2])
        path = self.ns["write_figure"]("t", "curve.png", fig)  # model passed a name WITH .png
        self.assertTrue(path.endswith("curve.png"))
        self.assertFalse(path.endswith("curve.png.png"))
        self.assertTrue((Path("outputs") / "t" / "curve.png").exists())
        self.assertFalse((Path("outputs") / "t" / "curve.png.png").exists())

    def test_write_figure_refuses_empty(self) -> None:
        self.ns["begin"]("t", {"seed": 1})
        empty = plt.figure()
        with self.assertRaises(ValueError):
            self.ns["write_figure"]("t", "empty", empty)

    def test_write_table_requires_a_row(self) -> None:
        self.ns["begin"]("t", {"seed": 1})
        with self.assertRaises(ValueError):
            self.ns["write_table"]("t", ["a"], [])

    def test_determinism_same_seed_same_draw(self) -> None:
        a = self.ns["begin"]("t", {"seed": 42}).standard_normal(5)
        b = self.ns["begin"]("t", {"seed": 42}).standard_normal(5)
        self.assertTrue(np.allclose(a, b))


class IoRuntimeInjectionTests(unittest.TestCase):
    def test_inject_writes_files_and_requirements(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "requirements.txt").write_text("numpy\n", encoding="utf-8")
            inject_io_runtime(root)
            self.assertTrue((root / "src" / "_io.py").exists())
            self.assertTrue((root / "src" / "__init__.py").exists())
            req = (root / "requirements.txt").read_text(encoding="utf-8")
            self.assertIn("numpy", req)
            self.assertIn("matplotlib", req)

    def test_ensure_requirements_is_idempotent(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "requirements.txt").write_text("numpy\nmatplotlib\n", encoding="utf-8")
            self.assertEqual(ensure_runtime_requirements(root), [])

    def test_ensure_requirements_adds_both_when_missing_file(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            added = ensure_runtime_requirements(root)
            self.assertEqual(sorted(added), ["matplotlib", "numpy"])


if __name__ == "__main__":
    unittest.main()
