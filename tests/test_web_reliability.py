from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from geng_agent.web.app import app
from geng_agent.web.db import init_database
from geng_agent.web.tasks import _is_transient


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


if __name__ == "__main__":
    unittest.main()
