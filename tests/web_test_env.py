from __future__ import annotations

import os
import tempfile
from pathlib import Path


WEB_TEST_ROOT = Path(tempfile.gettempdir()) / f"geng-agent-web-tests-{os.getpid()}"
WEB_TEST_ROOT.mkdir(parents=True, exist_ok=True)

os.environ["GENG_CASES_ROOT"] = str(WEB_TEST_ROOT)
os.environ["GENG_DATABASE_URL"] = f"sqlite:///{(WEB_TEST_ROOT / 'geng_web_tests.db').as_posix()}"
os.environ["GENG_CELERY_EAGER"] = "1"
