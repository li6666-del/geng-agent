"""Permission setup never elevates itself and leaves unrelated ACLs alone."""
import json
import os
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest

from geng_agent import windows_appcontainer_setup as setup

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows permission setup")


def test_unprivileged_apply_stops_before_opening_write_handles(monkeypatch):
    kernel = SimpleNamespace(CloseHandle=lambda *_: None)
    monkeypatch.setattr(setup, "_apis", lambda: (kernel, None, None, SimpleNamespace(IsUserAnAdmin=lambda: False)))
    monkeypatch.setattr(setup, "_open", lambda *_: pytest.fail("No system object may be opened for a rejected apply"))
    report = Path(r"D:\geng-artifacts\reports") / f"ac-setup-denied-{uuid.uuid4().hex[:8]}.json"
    assert setup.main(["--apply", "--report", str(report)]) == 1
    result = json.loads(report.read_text(encoding="utf-8"))
    assert not result["success"]
    assert result["objects"] == []
    assert "Administrator" in result["error"]


def test_host_preflight_only_reads_and_reports_missing_permissions(monkeypatch):
    observed = []
    kernel = SimpleNamespace(CloseHandle=lambda handle: observed.append(("close", handle)))
    monkeypatch.setattr(setup, "_apis", lambda: (kernel, None, None, None))
    def open_object(target, write, *_):
        assert write is False
        observed.append(("read", target["name"]))
        return target["name"]
    monkeypatch.setattr(setup, "_open", open_object)
    monkeypatch.setattr(setup, "_read", lambda *_: ("D:", []))
    result = setup.check_namespace_access()
    assert result["ready"] is False
    assert result["reason"] == "writer_boundary_setup_required"
    assert len(result["missing"]) == 5
    assert len([item for item in observed if item[0] == "close"]) == 5


def test_preflight_does_not_treat_deny_as_ready(monkeypatch):
    kernel = SimpleNamespace(CloseHandle=lambda *_: None)
    monkeypatch.setattr(setup, "_apis", lambda: (kernel, None, None, None))
    monkeypatch.setattr(setup, "_open", lambda target, *_: target)
    monkeypatch.setattr(setup, "_read", lambda target, *_: ("D:", [
        {"kind": 0, "mask": target["mask"], "flags": 0},
        {"kind": 1, "mask": 1, "flags": 0},
    ]))
    result = setup.check_namespace_access()
    assert not result["ready"]
    assert len(result["missing"]) == 5
