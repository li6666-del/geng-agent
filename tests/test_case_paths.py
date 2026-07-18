from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from geng_agent.config import get_cases_root, resolve_case_dir, validate_case_output_dir
from geng_agent.pipeline import ReviewPipeline


class CasePathTests(unittest.TestCase):
    def test_default_root_is_fixed_desktop_folder(self) -> None:
        with TemporaryDirectory() as temp_dir:
            home = Path(temp_dir) / "profile"
            expected = (home / "Desktop" / "耿同学agent_cases").resolve()
            with (
                patch("geng_agent.config.get_config_value", return_value=None),
                patch("geng_agent.config.Path.home", return_value=home),
            ):
                self.assertEqual(get_cases_root(), expected)

    def test_configured_root_wins(self) -> None:
        with TemporaryDirectory() as temp_dir:
            expected = (Path(temp_dir) / "cases").resolve()
            with patch("geng_agent.config.get_config_value", return_value=str(expected)):
                self.assertEqual(get_cases_root(), expected)

    def test_relative_case_name_resolves_below_case_root(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            with patch("geng_agent.config.get_cases_root", return_value=root):
                self.assertEqual(resolve_case_dir("case_demo"), root / "case_demo")

    def test_explicit_absolute_case_path_is_honored(self) -> None:
        with TemporaryDirectory() as temp_dir:
            target = Path(temp_dir).resolve() / "case_demo"
            self.assertEqual(resolve_case_dir(target), target)

    def test_rejects_nested_or_generated_text_paths(self) -> None:
        unsafe = (
            "../case_escape",
            "nested/case_demo",
            "- 把这一段说明文字建成目录",
            "系统会把原始论文以及 §analysis_warnings.json§ 复制到 sandbox。",
            "CON",
            "case.",
        )
        for raw in unsafe:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                resolve_case_dir(raw)

    def test_validator_accepts_short_unicode_case_name(self) -> None:
        with TemporaryDirectory() as temp_dir:
            target = Path(temp_dir).resolve() / "案例_001"
            self.assertEqual(validate_case_output_dir(target), target)

    def test_pipeline_rejects_bad_leaf_before_mkdir(self) -> None:
        with TemporaryDirectory() as temp_dir:
            bad = Path(temp_dir) / "- generated output text"
            with self.assertRaises(ValueError):
                ReviewPipeline().run(Path(temp_dir) / "missing.pdf", bad)
            self.assertFalse(bad.exists())


if __name__ == "__main__":
    unittest.main()