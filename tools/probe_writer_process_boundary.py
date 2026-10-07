"""Offline real-Codex probe: fixed localhost Responses, no model service.

The only termination targets are two dummy processes created by this function.
Pass domain.popen as launch_process/launch_peer to exercise process domains.
All caller-supplied output directories must live outside the checkout.
"""
from __future__ import annotations

import argparse
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
import uuid


class FixedResponsesServer:
    """Two HTTP replies: one fixed shell call, then a fixed final message."""

    def __init__(self, command: str, work_dir: Path, *, bind_host: str = "127.0.0.1"):
        self.command, self.work_dir = command, work_dir
        self.bind_host = bind_host
        self.requests = []
        self.errors = []
        self.tool_outputs = []
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                fixture.errors.append("unexpected GET " + self.path)
                self.send_error(404)

            def do_POST(self):
                if self.path != "/v1/responses":
                    fixture.errors.append("unexpected POST " + self.path)
                    self.send_error(404)
                    return
                try:
                    body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                    if self.headers.get("Content-Encoding") == "gzip":
                        body = gzip.decompress(body)
                    request = json.loads(body)
                    fixture.requests.append(request)
                    outputs = [item for item in request.get("input", [])
                               if item.get("type") in {"function_call_output", "local_shell_call_output"}]
                    fixture.tool_outputs.extend(outputs)
                    events = fixture.events(request, final=bool(outputs))
                    payload = "".join("event: " + item["type"] + "\ndata: " + json.dumps(item) + "\n\n"
                                      for item in events).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except Exception as exc:
                    fixture.errors.append(f"{type(exc).__name__}: {exc}")
                    self.send_error(500, "offline fixture error")

        self.server = ThreadingHTTPServer((bind_host, 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    @property
    def base_url(self):
        return f"http://{self.bind_host}:{self.server.server_port}/v1"

    def events(self, request, *, final):
        response_id = "resp_offline_" + str(len(self.requests))
        if final:
            item = {"id": "msg_offline", "type": "message", "role": "assistant", "status": "completed",
                    "content": [{"type": "output_text", "text": "OFFLINE_PROBE_FINISHED", "annotations": []}]}
        else:
            tools = {tool.get("name"): tool for tool in request.get("tools", []) if tool.get("type") == "function"}
            name = next((candidate for candidate in ("shell_command", "exec_command", "shell") if candidate in tools), None)
            if name is None:
                raise ValueError("No supported shell function advertised: " + repr(list(tools)))
            parameters = tools[name].get("parameters", {}).get("properties", {})
            if "cmd" in parameters:
                arguments = {"cmd": self.command, "workdir": str(self.work_dir), "max_output_tokens": 3000,
                             "yield_time_ms": 10000}
            elif "command" in parameters and parameters["command"].get("type") == "array":
                arguments = {"command": ["powershell.exe", "-NoProfile", "-Command", self.command],
                             "workdir": str(self.work_dir), "timeout_ms": 20000}
            else:
                arguments = {"command": self.command, "workdir": str(self.work_dir), "timeout_ms": 20000}
            arguments = {key: value for key, value in arguments.items() if key in parameters}
            item = {"id": "fc_offline", "type": "function_call", "call_id": "call_offline",
                    "name": name, "arguments": json.dumps(arguments), "status": "completed"}
        response = {"id": response_id, "object": "response", "created_at": int(time.time()),
                    "status": "completed", "model": request.get("model", "offline-probe"), "output": [item],
                    "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}
        return [
            {"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
            {"type": "response.output_item.added", "output_index": 0, "item": item},
            {"type": "response.output_item.done", "output_index": 0, "item": item},
            {"type": "response.completed", "response": response},
        ]


def _clean_env(work_dir: Path, *, fixture_host="127.0.0.1") -> dict[str, str]:
    allowed = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
               "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMDATA", "SYSTEMDRIVE", "HOMEDRIVE", "HOMEPATH"}
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    home = work_dir / "codex-home"
    home.mkdir(exist_ok=True)
    env.update({"CODEX_HOME": str(home), "GENG_OFFLINE_FIXTURE_KEY": "offline-not-a-real-key",
                "TEMP": str(work_dir), "TMP": str(work_dir), "PYTHONDONTWRITEBYTECODE": "1",
                "HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9",
                "ALL_PROXY": "http://127.0.0.1:9", "NO_PROXY": "127.0.0.1,localhost," + fixture_host})
    return env


def _finish_dummy(process):
    if process.poll() is None:
        if hasattr(process, "kill_tree"):
            process.kill_tree()
        else:
            process.kill()
    process.wait(timeout=10)


def run_codex_probe(work_dir: Path, *, codex: Path | None = None,
                    launch_process=None, launch_peer=None, timeout=60, check=True,
                    sandbox="workspace-write", bind_host="127.0.0.1") -> dict:
    """Create and clean up only our own dummy victims; never enumerate PIDs."""
    if os.name != "nt":
        raise RuntimeError("This probe exercises the Windows Writer boundary")
    if sandbox == "danger-full-access" and launch_process is None:
        raise ValueError("The probe only uses danger-full-access inside an explicit OS process domain")
    work_dir = work_dir.resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    executable = codex or Path(shutil.which("codex.exe") or "")
    if not executable.is_file():
        raise RuntimeError("Native codex.exe not found")
    spawn = launch_process or subprocess.Popen
    peer_spawn = launch_peer or subprocess.Popen
    dummy_command = [sys.executable, "-I", "-c", "import time; time.sleep(90)"]
    victim = subprocess.Popen(dummy_command, cwd=work_dir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        peer = peer_spawn(dummy_command, cwd=work_dir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except BaseException:
        _finish_dummy(victim)
        raise
    token = uuid.uuid4().hex
    (work_dir / "input.txt").write_text(token, encoding="utf-8")
    script = work_dir / "fixed_probe.py"
    script.write_text(
        "import ctypes,json,pathlib\n"
        "api=ctypes.WinDLL('kernel32',use_last_error=True)\n"
        "api.OpenProcess.argtypes=[ctypes.c_uint32,ctypes.c_int,ctypes.c_uint32]\n"
        "api.OpenProcess.restype=ctypes.c_void_p\n"
        "api.TerminateProcess.argtypes=[ctypes.c_void_p,ctypes.c_uint32]\n"
        "api.CloseHandle.argtypes=[ctypes.c_void_p]\n"
        "result={'own_file_read':pathlib.Path('input.txt').read_text()}\n"
        f"for name,pid in [('host_dummy',{victim.pid}),('peer_dummy',{peer.pid})]:\n"
        "    handle=api.OpenProcess(1,False,pid)\n"
        "    result[name]={'termination_handle_granted':bool(handle),'error':ctypes.get_last_error()}\n"
        "    if handle:\n"
        "        result[name]['terminated']=bool(api.TerminateProcess(handle,77))\n"
        "        api.CloseHandle(handle)\n"
        "pathlib.Path('probe_result.json').write_text(json.dumps(result),encoding='utf-8')\n"
        "print(json.dumps(result))\n", encoding="utf-8")
    quoted_python = str(Path(sys.executable)).replace("'", "''")
    command = f"& '{quoted_python}' -I -B fixed_probe.py"
    process = None
    try:
        with FixedResponsesServer(command, work_dir, bind_host=bind_host) as server:
            args = [str(executable), "-a", "never", "exec", "--ignore-user-config", "--ephemeral",
                    "--skip-git-repo-check", "--json", "-C", str(work_dir), "-s", sandbox,
                    "-m", "gpt-5.1-codex", "-c", 'model_provider="offline_fixture"',
                    "-c", 'windows.sandbox="unelevated"', "-c", 'web_search="disabled"',
                    "-c", "check_for_update_on_startup=false", "-c", "project_root_markers=[]",
                    "-c", "mcp_servers={}", "-c", "features.apps=false", "-c", "features.skills=false",
                    "-c", "features.multi_agent=false", "-c", "features.shell_snapshot=false",
                    "-c", "features.responses_websockets=false", "-c", "features.responses_websockets_v2=false",
                    "-c", 'model_providers.offline_fixture={name="offline fixture",base_url=' + json.dumps(server.base_url)
                    + ',wire_api="responses",env_key="GENG_OFFLINE_FIXTURE_KEY",requires_openai_auth=false,request_max_retries=0,stream_max_retries=0}',
                    "Run the local fixed probe once and report its output. Only its own two dummy processes are termination targets."]
            env = _clean_env(work_dir, fixture_host=bind_host)
            with (work_dir / "codex.stdout.log").open("w", encoding="utf-8") as stdout, \
                    (work_dir / "codex.stderr.log").open("w", encoding="utf-8") as stderr:
                process = spawn(args, cwd=work_dir, env=env, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr)
                timed_out = False
                try:
                    returncode = process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    _finish_dummy(process)
                    returncode = process.returncode
            result_path = work_dir / "probe_result.json"
            observed = json.loads(result_path.read_text(encoding="utf-8")) if result_path.is_file() else None
            report = {"codex_returncode": returncode, "timed_out": timed_out, "endpoint": server.base_url,
                      "requests_received": len(server.requests), "errors": server.errors,
                      "tool_outputs": server.tool_outputs, "observed": observed,
                      "host_dummy_alive": victim.poll() is None, "peer_dummy_alive": peer.poll() is None,
                      "file_roundtrip_ok": bool(observed and observed.get("own_file_read") == token)}
            (work_dir / "probe_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            if check:
                assert not timed_out and returncode == 0 and server.requests, report
                assert not server.errors and report["file_roundtrip_ok"], report
                assert report["host_dummy_alive"] and report["peer_dummy_alive"], report
                assert not observed["host_dummy"]["termination_handle_granted"], report
                assert not observed["peer_dummy"]["termination_handle_granted"], report
            return report
    finally:
        if process is not None and process.poll() is None:
            _finish_dummy(process)
        _finish_dummy(peer)
        _finish_dummy(victim)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--codex", type=Path)
    parser.add_argument("--observe-only", action="store_true")
    parser.add_argument("--bind-host", default="127.0.0.1", help="A local interface address; never a remote model server")
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from geng_agent.windows_process_boundary import WriterProcessDomain
    from geng_agent.windows_appcontainer_setup import check_namespace_access
    root = args.output.resolve()
    if not root.is_relative_to(Path(r"D:\geng-artifacts\tmp")):
        parser.error("Probe output must be a new directory under D:/geng-artifacts/tmp")
    root.mkdir(parents=True, exist_ok=False)
    compatibility = check_namespace_access()
    if not compatibility["ready"]:
        (root / "probe_report.json").write_text(json.dumps(compatibility, indent=2), encoding="utf-8")
        parser.exit(2, "Windows volume-query setup is required; no CLI or model was started.\n")
    writer_root, peer_root = root / "writer", root / "peer"
    writer_root.mkdir()
    peer_root.mkdir()
    executable = args.codex or Path(shutil.which("codex.exe") or "")
    with WriterProcessDomain(writer_root) as writer, WriterProcessDomain(peer_root) as peer:
        writer.allow_runtime_read(executable.resolve(strict=True).parent)
        result = run_codex_probe(writer_root, codex=executable, check=not args.observe_only,
            bind_host=args.bind_host, sandbox="danger-full-access",
            launch_process=writer.popen, launch_peer=peer.popen)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
