"""Observe/cancel owned model processes without imposing a thinking timeout."""
from __future__ import annotations

import os
import subprocess
import time

from .progress import PipelineCancelled


def _terminate_owned_tree(process):
    from .mineru_runner import _terminate_process_tree
    _terminate_process_tree(process)
    try:
        return process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        return process.communicate(timeout=5)


def run_observed_process(command, *, supervisor, label, cwd, env, input,
                         check_interval=0.5):
    supervisor._check_cancelled()
    flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
    with subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, encoding="utf-8", errors="replace", **flags) as process:
        started = time.monotonic()
        first = True
        try:
            supervisor.tools.observe_process(process.pid, label=label, status="running", output_bytes=0, elapsed_s=0)
            while True:
                supervisor._check_cancelled()
                try:
                    stdout, stderr = process.communicate(input=input if first else None, timeout=check_interval)
                    break
                except subprocess.TimeoutExpired as pending:
                    first = False
                    supervisor.tools.observe_process(process.pid, label=label, status="running",
                        elapsed_s=round(time.monotonic() - started, 1),
                        output_bytes=len(pending.output or b"") + len(pending.stderr or b""))
        except (PipelineCancelled, KeyboardInterrupt) as exc:
            stdout, stderr = _terminate_owned_tree(process)
            supervisor.tools.observe_process(process.pid, label=label, status="cancelled",
                returncode=process.returncode, elapsed_s=round(time.monotonic() - started, 1))
            exc.stdout, exc.stderr, exc.returncode = stdout, stderr, process.returncode
            raise
        except BaseException:
            _terminate_owned_tree(process)
            supervisor.tools.observe_process(process.pid, label=label, status="interrupted", returncode=process.returncode)
            raise
        supervisor.tools.observe_process(process.pid, label=label, status="completed", returncode=process.returncode,
            elapsed_s=round(time.monotonic() - started, 1), output_bytes=len(stdout.encode("utf-8")) + len(stderr.encode("utf-8")))
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
