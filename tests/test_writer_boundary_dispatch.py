"""Offline dispatch regressions; fake domains do not prove OS isolation."""
from __future__ import annotations

import json
import hashlib
import os
from dataclasses import replace
from pathlib import Path
import subprocess
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

from geng_agent import codex_runner, execution_receipts, task_writer_runner, writer_environment
from geng_agent.model_config import CodexModelConfig


class FakeProcess:
    """Only the observed-process protocol, with no real child process."""

    pid = 42001
    returncode = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def communicate(self, *, input=None, timeout=None):
        return "offline writer result", ""


class FakeDomain:
    policy = {"provider": "dispatch_test_double", "os_isolation_tested": False}

    def __init__(self, root, *, extra_write_roots=(), fail=None, runner=None):
        self.root = Path(root)
        self.extra_write_roots = tuple(extra_write_roots)
        self.fail = fail
        self.runner = runner
        self.calls = []
        self.writable = []
        self.readable = []
        self.entered = False
        self.exited = False

    def __enter__(self):
        if self.fail == "create":
            raise OSError("test isolation creation failure")
        self.entered = True
        return self

    def __exit__(self, *_args):
        self.exited = True
        return False

    def allow_write(self, path):
        self.writable.append(Path(path))

    def allow_read(self, path):
        self.readable.append(Path(path))

    allow_runtime_read = allow_read

    def run(self, command, **kwargs):
        self.calls.append(("run", list(command), kwargs))
        is_probe = command[-2:] == ["exec", "--help"]
        if self.fail == ("probe" if is_probe else "run"):
            raise OSError("test restricted launch failure")
        if self.runner is not None:
            return self.runner(command, **kwargs)
        stdout = "--ephemeral --ignore-user-config" if is_probe else "offline writer result"
        return subprocess.CompletedProcess(command, 0, stdout, "")

    def popen(self, command, **kwargs):
        self.calls.append(("popen", list(command), kwargs))
        if self.fail == "popen":
            raise OSError("test restricted launch failure")
        return FakeProcess()


@pytest.fixture
def no_plain_processes(monkeypatch):
    """A dispatch bypass fails even when its caller catches launch errors."""
    run = Mock(side_effect=AssertionError("unrestricted subprocess.run used"))
    popen = Mock(side_effect=AssertionError("unrestricted subprocess.Popen used"))
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(subprocess, "Popen", popen)
    yield run, popen
    run.assert_not_called()
    popen.assert_not_called()


@pytest.fixture
def domains(monkeypatch):
    instances = []
    settings = {}

    def create(root, *, extra_write_roots=()):
        domain = FakeDomain(root, extra_write_roots=extra_write_roots, **settings)
        instances.append(domain)
        return domain

    module = ModuleType("geng_agent.windows_process_boundary")
    module.WriterProcessDomain = create
    monkeypatch.setitem(sys.modules, module.__name__, module)
    return instances, settings


@pytest.fixture
def offline_codex(monkeypatch, tmp_path, no_plain_processes):
    codex_runner._clear_ephemeral_capability_cache()
    model = CodexModelConfig(
        provider="test", model="offline-model", base_url="https://invalid.example/v1",
        env_key="GENG_TEST_API_KEY", managed=True,
    )
    monkeypatch.setattr(codex_runner, "resolve_model_config", lambda *_args, **_kwargs: model)
    monkeypatch.setattr(codex_runner, "get_config_value", lambda *_args: None)
    monkeypatch.setattr(codex_runner, "codex_safe_env", lambda: {"GENG_TEST_API_KEY": "test-only"})
    monkeypatch.setattr(codex_runner.shutil, "which", lambda _name: str(tmp_path / "codex.exe"))
    monkeypatch.setattr(codex_runner, "session_started", lambda **_kwargs: None)
    monkeypatch.setattr(codex_runner, "session_finished", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("geng_agent.codex_cost.record_codex_invocation", lambda *_args, **_kwargs: {})
    monkeypatch.setattr("geng_agent.supervisor.current_supervisor", lambda: None)
    work = tmp_path / "writer"
    work.mkdir()

    def invoke(**kwargs):
        return codex_runner.run_codex_subprocess(
            role="task_writer", work_dir=work, audit_dir=tmp_path / "audit",
            prompt="offline dispatch probe", label="writer", sandbox="workspace-write", **kwargs,
        )

    yield invoke, work
    codex_runner._clear_ephemeral_capability_cache()


@pytest.mark.skipif(os.name != "nt", reason="Windows automatic domain creation")
def test_windows_writer_without_supervisor_uses_domain_for_probe_and_model(offline_codex, domains):
    invoke, work = offline_codex
    instances, _settings = domains

    result = invoke()

    assert result["ok"]
    assert len(instances) == 1
    domain = instances[0]
    assert domain.root == work and domain.entered and domain.exited
    assert [operation for operation, _command, _kwargs in domain.calls] == ["run", "run"]
    assert domain.calls[0][1][-2:] == ["exec", "--help"]
    command = domain.calls[1][1]
    assert command[command.index("--sandbox") + 1] == "danger-full-access"
    assert domain.calls[1][2]["input"] == "offline dispatch probe"
    assert result["process_isolation"] == domain.policy
    assert Path(result["last_message_path"]).parent in domain.writable


def test_supervised_writer_uses_domain_popen_not_host_popen(offline_codex, monkeypatch):
    invoke, work = offline_codex
    domain = FakeDomain(work)
    observations = Mock()
    supervisor = SimpleNamespace(tools=SimpleNamespace(observe_process=observations), _check_cancelled=lambda: None)
    monkeypatch.setattr("geng_agent.supervisor.current_supervisor", lambda: supervisor)

    result = invoke(process_boundary=domain)

    assert result["ok"]
    assert [operation for operation, _command, _kwargs in domain.calls] == ["run", "popen"]
    assert domain.calls[0][1][-2:] == ["exec", "--help"]
    assert observations.call_args.kwargs["status"] == "completed"


def test_host_selected_codex_authorizes_resolved_installation(offline_codex, monkeypatch):
    invoke, work = offline_codex
    domain = FakeDomain(work)
    exposed_executable = work.parent / "codex.exe"
    installed_executable = work.parent / "versioned-installation" / "codex.exe"
    original_resolve = Path.resolve

    def resolve(path, *args, **kwargs):
        if path == exposed_executable:
            return installed_executable
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    result = invoke(process_boundary=domain)

    assert result["ok"]
    assert installed_executable.parent in domain.readable
    assert exposed_executable.parent not in domain.readable


def test_catalog_is_copied_before_grant_without_authorizing_source(offline_codex, monkeypatch):
    invoke, work = offline_codex
    source = work.parent / "installer-owned-catalog.json"
    content = b'{"models": []}'
    source.write_bytes(content)
    original_model = replace(codex_runner.resolve_model_config(),
        model_catalog_json=source, model_catalog_sha256=hashlib.sha256(content).hexdigest())
    monkeypatch.setattr(codex_runner, "resolve_model_config", lambda *_args, **_kwargs: original_model)
    domain = FakeDomain(work)
    original_read = domain.allow_read
    original_write = domain.allow_write

    def allow_read(path):
        if Path(path) == source:
            raise PermissionError("source is readable but WRITE_DAC is denied")
        original_read(path)

    def allow_write(path):
        path = Path(path)
        if path.parent.name == "prompt_inputs":
            assert (path / "model_catalog.json").read_bytes() == content
        original_write(path)

    domain.allow_read = allow_read
    domain.allow_write = allow_write
    snapshots = []
    for _ in range(2):
        result = invoke(process_boundary=domain)
        assert result["ok"]
        snapshot = Path(result["model_catalog_snapshot"])
        snapshots.append(snapshot)
        command = domain.calls[-1][1]
        option = next(arg for arg in command if arg.startswith("model_catalog_json="))
        assert Path(json.loads(option.split("=", 1)[1])) == snapshot
        assert snapshot.parent in domain.writable
        assert snapshot.read_bytes() == content
        assert result["model_config"] == original_model.identity()
    assert snapshots[0] != snapshots[1]
    assert source.read_bytes() == content
    assert source not in domain.readable and source not in domain.writable
    assert original_model.model_catalog_json == source


@pytest.mark.parametrize("failure", ["probe", "run", "popen"])
def test_restricted_launch_error_never_falls_back_to_plain_process(offline_codex, monkeypatch, failure):
    invoke, work = offline_codex
    domain = FakeDomain(work, fail=failure)
    if failure == "popen":
        monkeypatch.setattr("geng_agent.supervisor.current_supervisor", lambda: SimpleNamespace(
            tools=SimpleNamespace(observe_process=lambda *_args, **_kwargs: None), _check_cancelled=lambda: None))

    result = invoke(process_boundary=domain)

    assert not result["ok"]
    assert result["error_kind"] == ("unsupported_cli_feature" if failure == "probe" else "subprocess_error")
    assert len(domain.calls) == (1 if failure == "probe" else 2)


def test_missing_windows_setup_is_explicit_and_does_not_start_a_model(offline_codex):
    invoke, work = offline_codex
    domain = FakeDomain(work)
    domain.check_codex_compatibility = lambda: {
        "ready": False, "reason": "writer_boundary_setup_required", "missing": ["test volume query"]}

    result = invoke(process_boundary=domain)

    assert not result["ok"]
    assert result["error_kind"] == "writer_boundary_setup_required"
    assert "管理员" in result["error"]
    assert not domain.calls


@pytest.mark.skipif(os.name != "nt", reason="Windows automatic domain creation")
def test_domain_creation_failure_prevents_writer_and_capability_launch(offline_codex, domains):
    invoke, _work = offline_codex
    instances, settings = domains
    settings["fail"] = "create"

    with pytest.raises(OSError, match="isolation creation failure"):
        invoke()

    assert len(instances) == 1
    assert instances[0].calls == []


@pytest.mark.skipif(sys.platform != "win32", reason="Windows session domain ownership")
def test_session_shares_one_domain_with_environment_broker_and_writer(monkeypatch, tmp_path, domains, no_plain_processes):
    sandbox = tmp_path / "writer"
    sandbox.mkdir()
    private_python = tmp_path / "private" / "Scripts" / "python.exe"
    runtime = SimpleNamespace(python_executable=tmp_path / "shared" / "python.exe",
        venv_dir=tmp_path / "shared", environment_hash="shared", lock={})
    captured = []

    def ensure(_sandbox, _runtime, *, process_boundary):
        assert process_boundary.entered and not process_boundary.exited
        captured.append(("environment", process_boundary))
        return private_python

    class Broker:
        session_id = "testsession"

        def __init__(self, *_args, **kwargs):
            captured.append(("broker", kwargs["process_boundary"]))

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def run_writer(**kwargs):
        captured.append(("writer", kwargs["process_boundary"]))
        return {"ok": True}

    monkeypatch.setattr(task_writer_runner, "writer_environment_root", lambda *_args: private_python.parent.parent)
    monkeypatch.setattr(task_writer_runner, "ensure_writer_environment", ensure)
    monkeypatch.setattr(task_writer_runner, "ExecutionBroker", Broker)
    monkeypatch.setattr(task_writer_runner, "run_codex_subprocess", run_writer)
    monkeypatch.setattr(task_writer_runner, "snapshot_writer_environment", lambda *_args: None)
    monkeypatch.setattr(task_writer_runner, "trusted_input_snapshot", lambda *_args: {})
    monkeypatch.setattr(task_writer_runner, "_inspect_task_writer_completion", lambda **kwargs: kwargs["status"])
    monkeypatch.setattr(task_writer_runner, "get_config_value", lambda *_args: None)

    result = task_writer_runner._run_task_writer_codex_session(
        label="writer", prompt="test", sandbox=sandbox, audit_dir=tmp_path / "audit", case_runtime=runtime)

    assert result["ok"]
    instances, _settings = domains
    assert len(instances) == 1
    domain = instances[0]
    assert domain.extra_write_roots == (private_python.parent.parent,)
    assert runtime.venv_dir in domain.readable
    assert captured == [("environment", domain), ("broker", domain), ("writer", domain)]
    assert domain.exited


@pytest.mark.skipif(sys.platform != "win32", reason="Windows session domain ownership")
def test_session_domain_failure_does_not_prepare_or_run_writer(monkeypatch, tmp_path, domains, no_plain_processes):
    _instances, settings = domains
    settings["fail"] = "create"
    prepare = Mock()
    model = Mock()
    broker = Mock()
    monkeypatch.setattr(task_writer_runner, "ensure_writer_environment", prepare)
    monkeypatch.setattr(task_writer_runner, "run_codex_subprocess", model)
    monkeypatch.setattr(task_writer_runner, "ExecutionBroker", broker)

    with pytest.raises(OSError, match="isolation creation failure"):
        task_writer_runner._run_task_writer_codex_session(
            label="writer", prompt="test", sandbox=tmp_path, audit_dir=tmp_path / "audit")

    prepare.assert_not_called()
    model.assert_not_called()
    broker.assert_not_called()


def test_environment_creation_and_restored_pip_use_domain_and_isolated_imports(monkeypatch, tmp_path, no_plain_processes):
    sandbox = tmp_path / "writer"
    sandbox.mkdir()
    shared = tmp_path / "shared"
    private = tmp_path / "private"
    shared_site = shared / "Lib" / "site-packages"
    shared_site.mkdir(parents=True)
    private_site = private / "Lib" / "site-packages"
    private_python = private / "Scripts" / "python.exe"
    runtime = SimpleNamespace(venv_dir=shared, python_executable=shared / "python.exe", environment_hash="base")
    (sandbox / "writer_environment.lock.json").write_text(json.dumps({
        "shared_python": str(runtime.python_executable.resolve()),
        "local_distributions": [{"name": "example-science", "version": "1.2"}],
    }), encoding="utf-8")

    def fake_run(command, **_kwargs):
        if command[1:4] == ["-I", "-m", "venv"]:
            private_site.mkdir(parents=True)
            private_python.parent.mkdir(parents=True)
            private_python.write_bytes(b"not an executable")
        return subprocess.CompletedProcess(command, 0, "", "")

    domain = FakeDomain(sandbox, runner=fake_run)
    monkeypatch.setattr(writer_environment, "writer_environment_root", lambda *_args: private)
    monkeypatch.setattr(writer_environment, "writer_python_path", lambda *_args: private_python)
    monkeypatch.setattr(writer_environment, "_site_packages", lambda root: root / "Lib" / "site-packages")

    selected = writer_environment.ensure_writer_environment(sandbox, runtime, process_boundary=domain)

    assert selected == private_python
    assert [call[1][1:4] for call in domain.calls] == [
        ["-I", "-m", "venv"], ["-I", "-S", "-c"],
        ["-I", "-m", "ensurepip"], ["-I", "-m", "pip"]]
    assert "--without-pip" in domain.calls[0][1]
    assert domain.calls[3][1][-2:] == ["install", "example-science==1.2"]
    assert json.loads(domain.calls[1][1][-1])[0] == [
        str(private_site / "geng_shared_science.pth"), str(shared_site.resolve()) + "\n"]
    assert all(call[2]["cwd"] == sandbox for call in domain.calls)


def test_package_inventory_probe_uses_given_boundary(tmp_path, no_plain_processes):
    inventory = {"python": "test-python", "packages": [["example-science", "1.2"]]}
    domain = FakeDomain(tmp_path, runner=lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, json.dumps(inventory), ""))

    result = execution_receipts.probe_execution_environment(tmp_path / "private-python.exe", process_boundary=domain)

    assert result["ok"] and result["inventory"] == inventory
    assert len(domain.calls) == 1
    assert domain.calls[0][1][1:3] == ["-I", "-c"]


@pytest.mark.skipif(os.name != "nt", reason="Windows automatic inventory boundary")
def test_package_inventory_without_domain_creates_its_own(monkeypatch, tmp_path, domains, no_plain_processes):
    instances, settings = domains
    settings["runner"] = lambda command, **_kwargs: subprocess.CompletedProcess(
        command, 0, '{"python":"test","packages":[]}', "")
    original_temporary = execution_receipts.tempfile.TemporaryDirectory
    monkeypatch.setattr(execution_receipts.tempfile, "TemporaryDirectory", lambda **kwargs: original_temporary(dir=tmp_path, **kwargs))

    result = execution_receipts.probe_execution_environment(tmp_path / "private-python.exe")

    assert result["ok"]
    assert len(instances) == 1
    assert instances[0].entered and instances[0].exited
    assert len(instances[0].calls) == 1


def test_package_inventory_launch_failure_is_observed_without_host_fallback(tmp_path, no_plain_processes):
    domain = FakeDomain(tmp_path, fail="run")

    result = execution_receipts.probe_execution_environment(tmp_path / "private-python.exe", process_boundary=domain)

    assert not result["ok"]
    assert "restricted launch failure" in result["error"]
    assert len(domain.calls) == 1
