import json
from pathlib import Path

import pytest

from geng_agent.project_portability import build_source_inventory, validate_repro_project_portability


def _validate_source(root: Path, source: str) -> dict:
    (root / "task.py").write_text(source, encoding="utf-8")
    (root / "source_inventory.json").write_text(
        json.dumps(build_source_inventory(root)), encoding="utf-8"
    )
    return validate_repro_project_portability(root, raise_on_error=False)


@pytest.mark.parametrize(
    "source",
    [
        'result = csv_path.replace("\\\\", "/")\n',
        'result = png_path.replace("/", "\\\\")\n',
        'output_path = str(named_csv.relative_to(root)).replace("\\\\", "/")\n',
        'open(csv_path.replace("\\\\", "/"))\n',
        'output_path = csv_path.replace("\\\\", "/", 1)\n',
    ],
)
def test_separator_normalization_is_not_an_absolute_root(tmp_path: Path, source: str) -> None:
    result = _validate_source(tmp_path, source)

    assert result["portable"], result["issues"]
    assert result["issues"] == []


@pytest.mark.parametrize(
    "source",
    [
        'open("/")\n',
        'from pathlib import Path\noutput_path = Path("/")\n',
        'output_path = csv_path.replace("relative", "/")\n',
        'output_path = csv_path.replace("\\\\", "/tmp/external")\n',
        'output_path = "/tmp/external".replace("\\\\", "/")\n',
        'output_path = "C:/outside/data.csv".replace("/", "\\\\")\n',
    ],
)
def test_source_path_expressions_are_preserved_without_static_runtime_verdict(
    tmp_path: Path, source: str
) -> None:
    result = _validate_source(tmp_path, source)

    assert result["portable"], result["issues"]
    assert result["smoke"]["ran"] is False
    assert (tmp_path / "task.py").read_text(encoding="utf-8") == source


def test_config_path_text_is_preserved_for_supervisor_review(tmp_path: Path) -> None:
    (tmp_path / "config.json").write_text('{"output_path":"/"}', encoding="utf-8")
    result = _validate_source(tmp_path, "")

    assert result["portable"], result["issues"]
    assert result["smoke"]["ran"] is False
    assert json.loads((tmp_path / "config.json").read_text(encoding="utf-8")) == {"output_path": "/"}
