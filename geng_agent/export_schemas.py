from __future__ import annotations

import argparse
import json
from pathlib import Path

from .benchmark_models import BenchmarkCase, BenchmarkReport, BenchmarkSuite
from .schema_models import export_json_schemas


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export Pydantic models to JSON Schema files.")
    parser.add_argument("--out", type=Path, default=Path("schemas"), help="Target schema directory.")
    args = parser.parse_args(argv)

    written = export_json_schemas(args.out)
    for stage, model in {
        "benchmark_case": BenchmarkCase,
        "benchmark_suite": BenchmarkSuite,
        "benchmark_report": BenchmarkReport,
    }.items():
        path = args.out / f"{stage}.schema.json"
        path.write_text(
            json.dumps(model.model_json_schema(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        written[stage] = path
    for stage, path in written.items():
        print(f"{stage}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
