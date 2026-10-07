"""Explicit administrator setup / read-only inspection for Writer AppContainers."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from geng_agent.windows_appcontainer_setup import main


if __name__ == "__main__":
    raise SystemExit(main())
