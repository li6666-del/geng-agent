from __future__ import annotations

import argparse
from pathlib import Path

from .schema_models import export_json_schemas


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export Pydantic models to JSON Schema files.")
    parser.add_argument("--out", type=Path, default=Path("schemas"), help="Target schema directory.")
    args = parser.parse_args(argv)

    written = export_json_schemas(args.out)
    for stage, path in written.items():
        print(f"{stage}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
