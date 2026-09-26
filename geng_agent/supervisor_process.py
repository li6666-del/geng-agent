"""Observe/cancel owned model processes without imposing a thinking timeout."""
from __future__ import annotations

import os
import subprocess
import time
from collections import deque
from threading import Thread

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
    observations = deque(maxlen=100)
    def observe(pid, **values):
        try:
            supervisor.tools.observe_process(pid, **values)
        except Exception as exc:
            observations.append(f"process observation unavailable: {type(exc).__name__}: {exc}")

    def check_cancelled():
        try:
            supervisor._check_cancelled()
        except PipelineCancelled:
            raise
        except Exception as exc:
            observations.append(f"cancellation observation unavailable: {type(exc).__name__}: {exc}")

    check_cancelled()
    flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
    with subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, encoding="utf-8", errors="replace", **flags) as process:
        started = time.monotonic()
        first = True
        try:
            observe(process.pid, label=label, status="running", output_bytes=0, elapsed_s=0)
            while True:
                check_cancelled()
                try:
                    stdout, stderr = process.communicate(input=input if first else None, timeout=check_interval)
                    break
                except subprocess.TimeoutExpired as pending:
                    first = False
                    observe(process.pid, label=label, status="running",
                        elapsed_s=round(time.monotonic() - started, 1),
                        output_bytes=len(pending.output or b"") + len(pending.stderr or b""))
        except (PipelineCancelled, KeyboardInterrupt) as exc:
            stdout, stderr = _terminate_owned_tree(process)
            observe(process.pid, label=label, status="cancelled",
                returncode=process.returncode, elapsed_s=round(time.monotonic() - started, 1))
            exc.stdout, exc.stderr, exc.returncode = stdout, stderr, process.returncode
            raise
        except Exception as exc:
            # A failed observer/pipe is not an instruction to terminate work.
            observations.append(f"process communication unavailable: {type(exc).__name__}: {exc}")
            captured = {"stdout": "", "stderr": ""}
            def drain(name):
                try:
                    stream = getattr(process, name)
                    if stream is not None and not stream.closed:
                        captured[name] = stream.read()
                except Exception as read_error:
                    observations.append(f"{name} capture unavailable: {read_error}")
            readers = [Thread(target=drain, args=(name,), daemon=True) for name in captured]
            for reader in readers:
                reader.start()
            if process.stdin is not None and not process.stdin.closed:
                try:
                    process.stdin.close()
                except OSError as close_error:
                    observations.append(f"stdin close unavailable: {close_error}")
            while process.poll() is None:
                try:
                    check_cancelled()
                    process.wait(timeout=check_interval)
                except subprocess.TimeoutExpired:
                    continue
                except (PipelineCancelled, KeyboardInterrupt):
                    _terminate_owned_tree(process)
                    raise
            for reader in readers:
                reader.join()
            stdout, stderr = captured["stdout"], captured["stderr"]
        observe(process.pid, label=label, status="completed", returncode=process.returncode,
            elapsed_s=round(time.monotonic() - started, 1), output_bytes=len(stdout.encode("utf-8")) + len(stderr.encode("utf-8")))
        completed = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        completed.observation_errors = list(observations)
        return completed
