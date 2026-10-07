import json
import os
from pathlib import Path
import sys
from unittest.mock import patch
from urllib.request import getproxies_environment, proxy_bypass_environment

import pytest

from geng_agent.execution_receipts import ExecutionBroker
from geng_agent.execution_sandbox import scientific_sandbox_launch
from geng_agent.security_env import build_safe_env


def test_safe_env_bypasses_system_proxy_without_inheriting_secrets(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "localhost")
    monkeypatch.setenv("no_proxy", "localhost")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only-placeholder")
    original = dict(os.environ)

    environment = build_safe_env()

    assert environment["NO_PROXY"] == environment["no_proxy"] == "*"
    assert not any(key.upper() in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "DEEPSEEK_API_KEY"}
                   for key in environment)
    assert dict(os.environ) == original
    with patch.dict(os.environ, environment, clear=True):
        assert getproxies_environment() == {"no": "*"}
        assert proxy_bypass_environment("downloads.example.org:443")


def test_scientific_launcher_keeps_direct_network_environment(tmp_path):
    executable = tmp_path / "codex.exe"
    executable.touch()
    with patch("geng_agent.execution_sandbox._sandbox_prefix",
               return_value=(str(executable), "sandbox")):
        launch = scientific_sandbox_launch(
            [sys.executable, "-c", "pass"], work_dir=tmp_path,
            write_roots=[tmp_path], env=build_safe_env(), codex_executable=executable,
        )
    assert launch["env"]["NO_PROXY"] == launch["env"]["no_proxy"] == "*"
    assert launch["policy"]["network_enabled"]


@pytest.mark.skipif(os.name != "nt", reason="Real Windows AppContainer execution")
def test_broker_and_scientific_child_inherit_direct_network(tmp_path, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "localhost")
    monkeypatch.setenv("no_proxy", "localhost")
    project = tmp_path / "project"
    tasks = project / "tasks"
    tasks.mkdir(parents=True)
    (tasks / "__init__.py").write_text("", encoding="utf-8")
    (project / "config.json").write_text("{}", encoding="utf-8")
    (project / "tasks_manifest.json").write_text(json.dumps({"tasks": [{
        "task_id": "sample", "module": "sample", "output_subdir": "sample",
        "config_full": "config.json",
    }]}), encoding="utf-8")
    probe = (
        "import json,os; from urllib.request import getproxies,proxy_bypass; "
        "print(json.dumps({'bypass':proxy_bypass('downloads.example.org:443'),"
        "'proxies':getproxies(),'no_proxy':os.environ.get('NO_PROXY')}))"
    )
    body = (
        "import json,os,subprocess,sys\n"
        "from pathlib import Path\n"
        "from urllib.request import getproxies,proxy_bypass\n"
        "def main(config):\n"
        f"    child=subprocess.run([sys.executable,'-I','-B','-c',{probe!r}],"
        "capture_output=True,text=True,check=True,timeout=20)\n"
        "    result={'parent':{'bypass':proxy_bypass('downloads.example.org:443'),"
        "'proxies':getproxies(),'no_proxy':os.environ.get('NO_PROXY')},"
        "'child':json.loads(child.stdout)}\n"
        "    Path('outputs/sample/network.json').write_text(json.dumps(result),encoding='utf-8')\n"
    )
    (tasks / "sample.py").write_text(body, encoding="utf-8")
    broker = ExecutionBroker(project, tmp_path / "audit", Path(sys.executable))

    receipt = broker.execute({"task_id": "sample", "mode": "full"})

    assert receipt["returncode"] == 0, receipt
    assert receipt["sandbox_policy"]["provider"] == "windows_appcontainer"
    assert receipt["sandbox_policy"]["process_isolation"]
    observed = json.loads((project / "outputs/sample/network.json").read_text(encoding="utf-8"))
    expected = {"bypass": True, "proxies": {"no": "*"}, "no_proxy": "*"}
    assert observed == {"parent": expected, "child": expected}
