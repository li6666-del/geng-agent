from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import delete

from geng_agent.web.app_v2 import app
from geng_agent.web.db import SessionLocal, init_database
from geng_agent.web.models import ArtifactRecord, CaseRecord, ExportRecord, JobEvent, JobRecord


class WebApiV2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        init_database()
        cls.client = TestClient(app)

    def setUp(self) -> None:
        with SessionLocal() as session:
            for model in (JobEvent, ArtifactRecord, ExportRecord, JobRecord, CaseRecord):
                session.execute(delete(model))
            session.commit()

    @patch("geng_agent.web.app_v2._dispatch_review")
    def test_create_case_and_enforce_one_active_job(self, dispatch) -> None:
        response = self.client.post(
            "/api/v1/cases",
            files={"pdf_file": ("paper.pdf", b"%PDF-1.4\nminimal", "application/pdf")},
            data={"display_name": "测试航行"},
        )
        self.assertEqual(response.status_code, 202, response.text)
        case_id = response.json()["case_id"]
        dispatch.assert_called_once()

        detail = self.client.get(f"/api/v1/cases/{case_id}")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(len(detail.json()["phases"]), 5)
        self.assertEqual(detail.headers["x-content-type-options"], "nosniff")

        duplicate = self.client.post(f"/api/v1/cases/{case_id}/jobs")
        self.assertEqual(duplicate.status_code, 409)

    def test_rejects_non_pdf_magic_bytes(self) -> None:
        response = self.client.post(
            "/api/v1/cases",
            files={"pdf_file": ("paper.pdf", b"not a pdf", "application/pdf")},
        )
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
