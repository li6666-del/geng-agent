"""Opt-in live provider probe followed by a tiny, synthetic production workflow.

Credentials come from the selected profile's environment variable or a hidden
terminal prompt. They are never accepted as command arguments or saved here.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from geng_agent.model_config import load_model_config, model_config_scope
from geng_agent.outputs import write_json
from geng_agent.security_env import redact_data, redact_text


PAPER = """# Synthetic BPSK AWGN validation note

This is an authored integration-test fixture, not a published research paper.
Its purpose is to exercise the reproduction workflow with a known answer.

## Model and scope
Use coherent uncoded BPSK over a real AWGN channel, equally probable symbols
x in {-1,+1}, received y=x+n and hard decision y>=0. Symbol energy and bit
energy are both 1. Let gamma_b=10**(EbN0_dB/10). Noise variance is
sigma_squared=1/(2*gamma_b). All SNR values below are Eb/N0, not Es/N0.

## Task 1: noise normalization
At Eb/N0=[0, 3, 6] dB, calculate the exact noise variance using the equation
above. Reference values are [0.5, 0.2505936168136361, 0.125594321575479].
The variance strictly decreases. Produce a CSV and a PNG plot.

## Task 2: theoretical BER
At the same three points, calculate the exact theoretical bit error rate
BER=0.5*erfc(sqrt(gamma_b)). The reference values are approximately
[0.07864960352514257, 0.02287840756108532, 0.0023882907809328075].
BER strictly decreases. Produce a CSV and a PNG plot.

## Reproduction scope
The two named tasks use the same BPSK energy and SNR normalization. Reuse this
normalization implementation. They have no dependency on one another's outputs.
Use double precision and the Python standard library for numerical calculations.
An installed plotting library may be used. No Monte Carlo simulation, training,
GPU, optimization, dependency installation or external dataset is necessary.
The listed numbers are rounded; agreement to 1e-8 absolute error is sufficient.
Only reproduce these two deterministic tasks. No confidence interval is needed
for the closed-form values. The note contains no original figure images: report
that fact and compare to its numerical values without inventing original plots.
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--prompt-key", action="store_true")
    parser.add_argument("--run-flow", action="store_true")
    args = parser.parse_args()
    config = load_model_config(args.config)
    if not config.managed or not config.base_url or not config.env_key:
        parser.error("This live probe requires an explicit API provider profile.")
    output = args.output.resolve()
    checkout = Path(__file__).resolve().parents[1]
    if output == checkout or checkout in output.parents:
        parser.error("Probe outputs must be outside the project checkout.")
    output.mkdir(parents=True, exist_ok=False)
    original_credential = os.environ.get(config.env_key)
    if args.prompt_key:
        if not sys.stdin.isatty():
            parser.error("A real terminal is required for hidden credential input.")
        os.environ[config.env_key] = getpass.getpass("Provider API key (hidden): ")
    credential = os.environ.get(config.env_key)
    if not credential:
        parser.error("Set the selected provider credential environment variable first.")
    os.environ.setdefault("GENG_PYTHON", sys.executable)
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    report = {"model_config": config.identity(), "synthetic_fixture": True,
              "started_at": time.time(), "checks": {}}

    def save() -> None:
        # Exact-value redaction also covers non-sk provider credential formats.
        safe = json.loads(json.dumps(redact_data(report), ensure_ascii=False).replace(credential, "[REDACTED]"))
        write_json(output / "probe_result.json", safe)

    def request(route: str, payload: dict | None = None) -> dict:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(config.base_url + route, data=data,
            headers={"Authorization": "Bearer " + credential, "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=90) as response:
            return json.load(response)

    try:
        print("Checking provider authentication and model availability...", flush=True)
        started = time.monotonic()
        try:
            models = request("/models")
            ids = [str(item.get("id")) for item in models.get("data", [])]
            report["checks"]["models"] = {"ok": config.model in ids, "available_models": ids,
                "duration_s": round(time.monotonic() - started, 2)}
            save()
            if config.model not in ids:
                print("Requested model is not available for this credential.", flush=True)
                return 2
        except urllib.error.HTTPError as exc:
            report["checks"]["models"] = {"ok": False, "http_status": exc.code,
                "error": redact_text(exc.read(8192).decode("utf-8", errors="replace")).replace(credential, "[REDACTED]")}
            save()
            print(f"Provider authentication/model listing failed: HTTP {exc.code}", flush=True)
            return 2

        print("Checking Responses API at the selected reasoning effort...", flush=True)
        started = time.monotonic()
        response = request("/responses", {"model": config.model,
            "input": "Reply with only the exact text CONNECTION_OK.",
            "reasoning": {"effort": config.reasoning_effort}, "max_output_tokens": 2048})
        text = "".join(part.get("text", "") for item in response.get("output", [])
                       for part in item.get("content", []) if part.get("type") == "output_text")
        report["checks"]["responses"] = {"ok": text.strip() == "CONNECTION_OK",
            "response_model": response.get("model"), "response_status": response.get("status"),
            "text": text, "usage": response.get("usage"), "duration_s": round(time.monotonic() - started, 2)}
        save()
        if not report["checks"]["responses"]["ok"]:
            return 3

        from geng_agent.codex_runner import run_codex_subprocess
        workspace = output / "connection_workspace"
        workspace.mkdir()
        marker = "EXECUTION_OK_" + uuid4().hex
        probe_source = ('from pathlib import Path\n'
                        'Path("connection.json").write_text(\'{"connected":true}\', encoding="utf-8")\n'
                        f'print({marker!r})\n')
        (workspace / "execution_probe.py").write_text(probe_source, encoding="utf-8")
        with model_config_scope(config):
            print("Checking the project's Codex transport and actual Python execution...", flush=True)
            status = run_codex_subprocess(role="analysis", work_dir=workspace,
                prompt=(f'Using your shell tool, run the existing execution_probe.py with this Python: {sys.executable}. '
                        'Do not edit any file or read other directories. This tiny script creates connection.json '
                        'and prints an execution marker. If command execution is blocked, report the error and stop; '
                        'do not substitute file editing for execution or retry sandbox setup. Then stop.'),
                audit_dir=output / "connection_audit", label="connection", sandbox="workspace-write")
            file_ok = False
            try:
                file_ok = json.loads((workspace / "connection.json").read_text(encoding="utf-8-sig")) == {"connected": True}
            except (OSError, ValueError):
                pass
            command_ok = False
            transcript = Path(status.get("transcript") or output / "missing_transcript")
            if transcript.is_file():
                for line in transcript.read_text(encoding="utf-8").splitlines():
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    item = event.get("item", {})
                    if not isinstance(item, dict):
                        continue
                    if (event.get("type") == "item.completed"
                            and item.get("type") == "command_execution" and item.get("exit_code") == 0
                            and marker in item.get("aggregated_output", "")):
                        command_ok = True
            source_unchanged = (workspace / "execution_probe.py").read_text(encoding="utf-8") == probe_source
            report["checks"]["codex"] = {"ok": bool(status.get("ok") and file_ok and command_ok and source_unchanged),
                "file_ok": file_ok, "command_ok": command_ok, "probe_source_unchanged": source_unchanged,
                "status": status}
            save()
            if not report["checks"]["codex"]["ok"]:
                print("Codex transport/execution check failed; see probe_result.json.", flush=True)
                return 4
            if args.run_flow:
                from geng_agent.pipeline import ReviewPipeline
                from geng_agent.progress import ConsoleProgressReporter
                paper = output / "synthetic_bpsk.md"
                paper.write_text(PAPER, encoding="utf-8")
                print("Starting the real pipeline on the synthetic BPSK fixture...", flush=True)
                started = time.monotonic()
                result = ReviewPipeline().run(paper, output / "case", run_repro=True,
                    resume=False, analysis_fallback=False, model_config_path=args.config,
                    progress=ConsoleProgressReporter())
                report["checks"]["pipeline"] = {"returned": True,
                    "runtime_passed": result.runtime_passed,
                    "result_review_passed": result.result_review_passed,
                    "duration_s": round(time.monotonic() - started, 2),
                    "output_dir": str(result.output_dir)}
                save()
        print("Live probe complete. Evidence: " + str(output / "probe_result.json"), flush=True)
        return 0
    except KeyboardInterrupt:
        report["error"] = "Interrupted before the live probe completed."
        save()
        print("Probe interrupted; completed evidence has been saved.", flush=True)
        return 130
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        if isinstance(exc, urllib.error.HTTPError):
            detail += " " + exc.read(8192).decode("utf-8", errors="replace")
        report["error"] = redact_text(detail).replace(credential, "[REDACTED]")
        save()
        print("Probe stopped: " + report["error"], flush=True)
        return 5
    finally:
        if original_credential is None:
            os.environ.pop(config.env_key, None)
        else:
            os.environ[config.env_key] = original_credential


if __name__ == "__main__":
    raise SystemExit(main())
