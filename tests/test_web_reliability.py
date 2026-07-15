from __future__ import annotations

import unittest
from unittest.mock import patch

from tests import web_test_env  # noqa: F401

from fastapi.testclient import TestClient

from geng_agent.web.app import app
from geng_agent.web.app_v2 import _dispatch_review
from geng_agent.web.db import init_database
from geng_agent.web.events import event_cursor
from geng_agent.web.tasks import _is_transient, run_review


class WebReliabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        init_database()
        cls.client = TestClient(app)

    def test_transient_error_classification_is_bounded(self) -> None:
        self.assertTrue(_is_transient(TimeoutError("upstream timeout")))
        self.assertTrue(_is_transient(RuntimeError("LLM request failed: HTTP 503")))
        self.assertFalse(_is_transient(ValueError("schema validation failed")))

    def test_prometheus_metrics_and_security_headers(self) -> None:
        response = self.client.get("/api/v1/metrics")
        self.assertEqual(response.status_code, 200)
        self.assertIn("geng_queue_depth", response.text)
        self.assertIn("geng_job_total", response.text)
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")

    def test_sse_cursor_ignores_malformed_browser_header(self) -> None:
        self.assertEqual(event_cursor(7, "not-an-integer"), 7)
        self.assertEqual(event_cursor(7, "12"), 12)

    def test_eager_dispatch_uses_celery_entrypoint_so_retries_work(self) -> None:
        with patch.object(run_review, "delay") as delay, patch("geng_agent.web.app_v2.threading.Thread") as thread:
            _dispatch_review("job-123")

        self.assertIs(thread.call_args.kwargs["target"], delay)
        thread.return_value.start.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
