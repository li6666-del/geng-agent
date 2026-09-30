"""Result views use only the delivered ZIP; no model or science process runs."""
from __future__ import annotations

import io
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote

import pytest
from docx import Document
from fastapi.testclient import TestClient

from tests import web_test_env  # noqa: F401
from tests.test_web_portal import client, database, register, upload  # noqa: F401
from geng_agent.web import result_details as details_module
from geng_agent.web.app import app
from geng_agent.web.db import SessionLocal
from geng_agent.web.delivery import DELIVERY_ROOT, REPORTS, bundle_path
from geng_agent.web.models import JobRecord


def word_bytes(*paragraphs: str) -> bytes:
    stream = io.BytesIO()
    document = Document()
    for paragraph in paragraphs:
        document.add_paragraph(paragraph)
    document.save(stream)
    return stream.getvalue()


def delivered(client, entries=None):
    register(client)
    case_id, job_id, root = upload(client, "通信成果测试")
    comparison = word_bytes("通信论文复现结果对比报告", "智能体原文：主要趋势得到支持。", "仍需关注样本量。")
    reproduction = word_bytes("本地复现报告", "运行命令和参数说明")
    prefix = f"{DELIVERY_ROOT}/复现任务/t01_T1/"
    default = {
        REPORTS["result_review.docx"]: comparison,
        REPORTS["reproduction_report.docx"]: reproduction,
        prefix + "代码/": b"",
        prefix + "代码/channel.py": b"# channel model",
        prefix + "代码/algorithms/main.py": b"# simulation",
        prefix + "复现结果/": b"",
        prefix + "复现结果/curve.csv": b"x,y\n1,2",
        prefix + "readme.md": "# 信道误码率实验\n\n代码/channel.py：构建衰落信道。\n复现结果/curve.csv：误码率曲线数据。".encode(),
    }
    path = bundle_path(root, job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in (default if entries is None else entries).items():
            archive.writestr(name, content)
    with SessionLocal() as session:
        job = session.get(JobRecord, job_id)
        job.status = "succeeded"
        job.finished_at = datetime.now(timezone.utc)
        session.commit()
    return case_id, job_id, root, path, comparison, reproduction


def test_cloud_only_archive_exposes_reports_tasks_and_original_excerpt(client):
    case_id, _job, root, path, comparison, reproduction = delivered(client)
    assert not (root / "result_review.docx").exists()
    assert not (root / "repro_project").exists()
    response = client.get(f"/api/v1/cases/{case_id}/result")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    result = response.json()
    assert result["available"] is True and result["finished_at"]
    assert datetime.fromisoformat(result["finished_at"]).tzinfo is not None
    assert result["bundle"]["size_bytes"] == path.stat().st_size
    assert result["excerpt"] == ["通信论文复现结果对比报告", "智能体原文：主要趋势得到支持。", "仍需关注样本量。"]
    assert [(item["id"], item["size_bytes"]) for item in result["reports"]] == [
        ("comparison", len(comparison)), ("reproduction", len(reproduction)),
    ]
    assert len(result["tasks"]) == 1
    task = result["tasks"][0]
    assert task["directory"] == "t01_T1" and task["name"] == "信道误码率实验"
    assert task["code_files"] == 2 and task["result_files"] == 1
    assert "构建衰落信道" in task["readme"]
    assert str(root) not in response.text


@pytest.mark.parametrize("report_id,position", [("comparison", 4), ("reproduction", 5)])
def test_single_report_download_preserves_bytes_and_safe_filename(client, report_id, position):
    fixture = delivered(client)
    response = client.get(f"/api/v1/cases/{fixture[0]}/reports/{report_id}")
    assert response.status_code == 200 and response.content == fixture[position]
    assert response.headers["content-type"] == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    assert "attachment; filename*=UTF-8''" in response.headers["content-disposition"]
    assert ".docx" in unquote(response.headers["content-disposition"])
    assert response.headers["cache-control"] == "no-store"


def test_report_routes_require_ownership_and_authentication(client):
    case_id, *_ = delivered(client)
    with TestClient(app) as other:
        for suffix in ("result", "reports/comparison", "reports/reproduction"):
            assert other.get(f"/api/v1/cases/{case_id}/{suffix}").status_code == 401
        register(other, "other-detail@example.com")
        for suffix in ("result", "reports/comparison", "reports/reproduction"):
            assert other.get(f"/api/v1/cases/{case_id}/{suffix}").status_code == 404


@pytest.mark.parametrize("status", ["queued", "running", "failed", "cancelled"])
def test_unfinished_latest_job_never_exposes_previous_delivery(client, status):
    case_id, _job, *_ = delivered(client)
    with SessionLocal() as session:
        session.add(JobRecord(id=str(uuid.uuid4()), case_id=case_id, status=status,
                              created_at=datetime.now(timezone.utc) + timedelta(seconds=1)))
        session.commit()
    result = client.get(f"/api/v1/cases/{case_id}/result").json()
    assert result["available"] is False and result["message"]
    assert result["bundle"] is None and result["reports"] == [] and result["excerpt"] == []
    assert client.get(f"/api/v1/cases/{case_id}/reports/comparison").status_code == 409


def test_missing_archive_has_readable_unavailable_state(client):
    case_id, _job, _root, path, *_ = delivered(client)
    path.unlink()
    response = client.get(f"/api/v1/cases/{case_id}/result")
    assert response.status_code == 200
    assert response.json()["available"] is False and response.json()["bundle"] is None
    assert "交付包暂不可用" in response.json()["message"]
    assert client.get(f"/api/v1/cases/{case_id}/reports/comparison").status_code == 404


def test_broken_zip_does_not_change_status_or_disable_existing_download(client):
    case_id, job_id, _root, path, *_ = delivered(client)
    path.write_bytes(b"damaged zip fixture")
    result = client.get(f"/api/v1/cases/{case_id}/result").json()
    assert result["available"] is False and "详情暂时无法读取" in result["message"]
    download = client.get(result["bundle"]["download_url"])
    assert download.status_code == 200 and download.content == b"damaged zip fixture"
    assert client.get(f"/api/v1/cases/{case_id}/reports/comparison").status_code == 404
    with SessionLocal() as session:
        assert session.get(JobRecord, job_id).status == "succeeded"


def test_broken_docx_leaves_report_and_archive_downloads_available(client):
    content = b"invalid docx fixture"
    case_id, *_ = delivered(client, {REPORTS["result_review.docx"]: content})
    result = client.get(f"/api/v1/cases/{case_id}/result").json()
    assert result["available"] is True and result["excerpt"] == []
    assert client.get(result["reports"][0]["download_url"]).content == content
    assert client.get(result["bundle"]["download_url"]).status_code == 200


def test_corrupt_compressed_report_preserves_archive_download(client):
    case_id, _job, _root, path, *_ = delivered(client)
    with zipfile.ZipFile(path) as archive:
        info = archive.getinfo(REPORTS["result_review.docx"])
        offset = info.header_offset + 30 + len(info.filename.encode("utf-8")) + len(info.extra)
    payload = bytearray(path.read_bytes())
    payload[offset] = 0xFF  # An invalid DEFLATE block; the archive directory remains readable.
    path.write_bytes(payload)
    result = client.get(f"/api/v1/cases/{case_id}/result").json()
    assert result["available"] is True and result["excerpt"] == []
    assert client.get(f"/api/v1/cases/{case_id}/reports/comparison").status_code == 404
    assert client.get(result["bundle"]["download_url"]).status_code == 200


def test_intermediate_reports_and_arbitrary_archive_paths_are_not_exposed(client):
    case_id, _job, root, *_ = delivered(client, {
        "audit/result_review.docx": word_bytes("中间审查不应展示"),
        "../result_review.docx": word_bytes("越界条目不应展示"),
        f"{DELIVERY_ROOT}/复现任务/../代码/private.txt": b"private",
    })
    (root / "result_review.docx").write_bytes(word_bytes("本地未交付报告不应展示"))
    result = client.get(f"/api/v1/cases/{case_id}/result").json()
    assert result["reports"] == [] and result["excerpt"] == [] and result["tasks"] == []
    assert client.get(f"/api/v1/cases/{case_id}/reports/comparison").status_code == 404
    for report_id in ("result_review.docx", "audit", "..%2Fresult_review.docx"):
        assert client.get(f"/api/v1/cases/{case_id}/reports/{report_id}").status_code == 404


def test_readme_is_bounded_and_kept_as_text(client, monkeypatch):
    case_id, *_ = delivered(client)
    monkeypatch.setattr(details_module, "MAX_README_BYTES", 10)
    result = client.get(f"/api/v1/cases/{case_id}/result").json()
    assert result["available"] is True and result["tasks"][0]["readme"] is None
    assert result["tasks"][0]["name"] == "t01_T1"


def test_large_report_download_has_limit_but_whole_bundle_stays_available(client, monkeypatch):
    case_id, *_ = delivered(client)
    monkeypatch.setattr(details_module, "MAX_REPORT_BYTES", 10)
    result = client.get(f"/api/v1/cases/{case_id}/result").json()
    assert result["available"] is True and result["excerpt"] == []
    assert client.get(f"/api/v1/cases/{case_id}/reports/comparison").status_code == 413
    assert client.get(result["bundle"]["download_url"]).status_code == 200


def test_excerpt_limits_output_and_nested_xml_size(client, monkeypatch):
    document = word_bytes(*[f"第{i}段" + "正文" * 250 for i in range(12)])
    case_id, *_ = delivered(client, {REPORTS["result_review.docx"]: document})
    result = client.get(f"/api/v1/cases/{case_id}/result").json()
    assert sum(map(len, result["excerpt"])) == 1600
    assert len(result["excerpt"]) <= 8 and result["excerpt"][0].startswith("第0段")
    monkeypatch.setattr(details_module, "MAX_DOCUMENT_XML_BYTES", 10)
    result = client.get(f"/api/v1/cases/{case_id}/result").json()
    assert result["excerpt"] == [] and result["reports"]
