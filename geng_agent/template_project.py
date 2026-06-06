from __future__ import annotations

import json
import re
from typing import Any


BER_TEMPLATE_NAME = "deterministic_ber_curve_v1"
ACCURACY_TEMPLATE_NAME = "deterministic_accuracy_curve_v1"
GENERIC_TEMPLATE_NAME = "deterministic_generic_metric_v1"


def choose_template_name(facts: dict[str, Any], tasks: dict[str, Any]) -> str:
    first_task = _first_task(tasks)
    task_text = " ".join(
        str(first_task.get(key) or "")
        for key in ("metric", "figure_or_claim", "objective", "title", "description")
    )
    all_text = f"{task_text}\n{json.dumps({'facts': facts, 'tasks': tasks}, ensure_ascii=False)}"

    if _has_metric_term(task_text, ("ber", "ser", "bler", "snr")) or _has_metric_term(
        all_text, ("bit error rate", "symbol error rate", "block error rate", "signal to noise")
    ):
        return BER_TEMPLATE_NAME
    if _has_metric_term(task_text, ("accuracy", "classification", "precision", "recall")):
        return ACCURACY_TEMPLATE_NAME
    return GENERIC_TEMPLATE_NAME


def build_template_repro_project_manifest(
    *,
    facts: dict[str, Any],
    tasks: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    template_name = choose_template_name(facts, tasks)
    config = _build_config(facts, tasks)
    config["template_name"] = template_name
    smoke_config = {
        **config,
        "task_id": f"{config['task_id']}_smoke",
        "num_bits": min(int(config.get("num_bits", 4000)), 4000),
        "snr_db": _smoke_snr(config["snr_db"]),
        "modulations": config["modulations"][:2],
        "channels": config["channels"][:2],
    }
    assumptions = list(config.get("assumptions", []))
    assumptions.extend(
        [
            {
                "name": "template_fallback",
                "value": reason,
                "risk": "medium",
                "reason": f"The generated project uses deterministic local template fallback `{template_name}` because free-form LLM code generation or repair was not reliable enough.",
            },
            {
                "name": "template_name",
                "value": template_name,
                "risk": "low",
                "reason": "Records which deterministic fallback template was selected for this reproduction project.",
            },
        ]
    )
    config["assumptions"] = assumptions
    smoke_config["assumptions"] = assumptions

    files = {
        "README.md": _readme(config),
        "requirements.txt": "numpy\nmatplotlib\n",
        "config.json": json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        "config_smoke.json": json.dumps(smoke_config, ensure_ascii=False, indent=2) + "\n",
        "run_experiment.py": RUN_EXPERIMENT,
        "src/__init__.py": "",
        "src/channel.py": CHANNEL_PY,
        "src/modulation.py": MODULATION_PY,
        "src/metrics.py": METRICS_PY,
        "src/simulation.py": SIMULATION_PY,
    }
    return {
        "files": [{"path": path, "content_lines": content.splitlines()} for path, content in files.items()],
        "_meta": {
            "template_fallback_used": True,
            "template_fallback_reason": reason,
            "template_name": template_name,
        },
    }


def _build_config(facts: dict[str, Any], tasks: dict[str, Any]) -> dict[str, Any]:
    first_task = _first_task(tasks)
    blob = json.dumps({"facts": facts, "tasks": tasks}, ensure_ascii=False)
    modulations = _extract_modulations(blob)
    channels = _extract_channels(blob)
    snr_db = _extract_snr_values(facts, blob)
    return {
        "task_id": str(first_task.get("task_id") or "template_reproduce_ber_vs_snr"),
        "metric": str(first_task.get("metric") or "bit_error_rate"),
        "num_bits": _extract_num_bits(facts) or 20000,
        "snr_db": snr_db,
        "modulations": modulations,
        "channels": channels,
        "seed": 42,
        "assumptions": _task_assumptions(first_task)
        + [
            {
                "name": "minimal_signal_chain",
                "value": "Analytic BER approximation plus seeded binomial sampling.",
                "risk": "medium",
                "reason": "The paper does not provide enough implementation detail for a full PHY stack in a stable smoke run.",
            }
        ],
    }


def _first_task(tasks: dict[str, Any]) -> dict[str, Any]:
    repro_tasks = tasks.get("repro_tasks")
    if isinstance(repro_tasks, list) and repro_tasks and isinstance(repro_tasks[0], dict):
        return repro_tasks[0]
    return {}


def _has_metric_term(text: str, terms: tuple[str, ...]) -> bool:
    for term in terms:
        pattern = r"(?<![A-Za-z0-9])" + re.escape(term).replace(r"\ ", r"\s+") + r"(?![A-Za-z0-9])"
        if re.search(pattern, text, re.IGNORECASE):
            return True
    return False


def _task_assumptions(task: dict[str, Any]) -> list[dict[str, Any]]:
    assumptions = task.get("assumptions")
    if not isinstance(assumptions, list):
        return []
    return [item for item in assumptions if isinstance(item, dict)]


def _extract_modulations(blob: str) -> list[str]:
    ordered = []
    for name in ("BPSK", "QPSK", "16-QAM", "64-QAM"):
        if re.search(re.escape(name), blob, re.IGNORECASE):
            ordered.append(name)
    if re.search(r"\bQAM\b", blob, re.IGNORECASE) and not ordered:
        ordered.append("BPSK")
    return ordered or ["BPSK", "16-QAM"]


def _extract_channels(blob: str) -> list[str]:
    channels = []
    for name in ("AWGN", "Rician", "Rayleigh"):
        if re.search(re.escape(name), blob, re.IGNORECASE):
            channels.append(name)
    return channels or ["AWGN", "Rayleigh"]


def _extract_snr_values(facts: dict[str, Any], blob: str) -> list[int]:
    for fact in facts.get("engineering_facts", []) if isinstance(facts.get("engineering_facts"), list) else []:
        if not isinstance(fact, dict):
            continue
        value = fact.get("value")
        if not isinstance(value, dict):
            continue
        keys = {str(key).lower(): val for key, val in value.items()}
        min_value = _to_int(keys.get("snr_min_db") or keys.get("snr_min") or keys.get("ebno_min_db"))
        max_value = _to_int(keys.get("snr_max_db") or keys.get("snr_max") or keys.get("ebno_max_db"))
        if min_value is not None and max_value is not None and min_value < max_value:
            step = max(1, min(5, (max_value - min_value) // 6 or 1))
            return list(range(min_value, max_value + 1, step))
    match = re.search(r"(?i)(?:snr|eb/no|ebno)[^0-9-]{0,30}(-?\d+)\s*(?:to|-|~)\s*(-?\d+)", blob)
    if match:
        start, stop = int(match.group(1)), int(match.group(2))
        if start < stop:
            step = max(1, min(5, (stop - start) // 6 or 1))
            return list(range(start, stop + 1, step))
    return [0, 5, 10, 15, 20, 25, 30]


def _extract_num_bits(facts: dict[str, Any]) -> int | None:
    for fact in facts.get("engineering_facts", []) if isinstance(facts.get("engineering_facts"), list) else []:
        if not isinstance(fact, dict):
            continue
        value = fact.get("value")
        if not isinstance(value, dict):
            continue
        for key, raw in value.items():
            if "bit" in str(key).lower():
                number = _to_int(raw)
                if number and number > 0:
                    return number
    return None


def _smoke_snr(values: list[int]) -> list[int]:
    if len(values) <= 5:
        return values
    return [values[0], values[len(values) // 4], values[len(values) // 2], values[-2], values[-1]]


def _to_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    match = re.search(r"-?\d+", str(value))
    return int(match.group(0)) if match else None


def _readme(config: dict[str, Any]) -> str:
    return f"""# Template Reproduction Project

This project is a deterministic fallback implementation for `{config['task_id']}`.

Selected template: `{config.get('template_name', BER_TEMPLATE_NAME)}`.

Run:

```bash
python run_experiment.py config_smoke.json
```

It generates:

- `outputs/results.csv`
- `outputs/ber_comparison.png`
- `outputs/summary.json`

The template is intentionally small and records its simplifying assumptions in `summary.json`.
"""


RUN_EXPERIMENT = r'''from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from src.simulation import run_simulation


def main() -> int:
    config_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("config_smoke.json")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    output_dir = Path("outputs")
    output_dir.mkdir(exist_ok=True)
    rows = run_simulation(config)

    csv_path = output_dir / "results.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["snr_db", "modulation", "channel", "bit_error_rate", "bit_errors", "num_bits"])
        writer.writeheader()
        writer.writerows(rows)

    plot_path = output_dir / "ber_comparison.png"
    plot_results(rows, plot_path)

    summary = {
        "task_id": config.get("task_id"),
        "metrics": {"primary": config.get("metric", "bit_error_rate"), "rows": len(rows)},
        "results": {"csv": "outputs/results.csv", "png": "outputs/ber_comparison.png"},
        "assumptions": config.get("assumptions", []),
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"generated {csv_path} and {plot_path}")
    return 0


def plot_results(rows, path):
    plt.figure(figsize=(8, 5))
    for modulation in sorted({row["modulation"] for row in rows}):
        for channel in sorted({row["channel"] for row in rows}):
            subset = [row for row in rows if row["modulation"] == modulation and row["channel"] == channel]
            if not subset:
                continue
            plt.semilogy(
                [row["snr_db"] for row in subset],
                [max(row["bit_error_rate"], 1e-8) for row in subset],
                marker="o",
                label=f"{modulation}/{channel}",
            )
    plt.xlabel("SNR (dB)")
    plt.ylabel("Bit Error Rate")
    plt.grid(True, which="both", alpha=0.3)
    plt.legend(fontsize=8)
    plt.tight_layout()
    plt.savefig(path, dpi=150)
    plt.close()


if __name__ == "__main__":
    raise SystemExit(main())
'''


CHANNEL_PY = r'''CHANNEL_FACTORS = {
    "AWGN": 1.0,
    "Rician": 0.55,
    "Rayleigh": 0.28,
}


def channel_factor(name: str) -> float:
    return CHANNEL_FACTORS.get(name, 0.7)
'''


MODULATION_PY = r'''MODULATION_ORDER = {
    "BPSK": 2,
    "QPSK": 4,
    "QAM": 2,
    "16-QAM": 16,
    "64-QAM": 64,
}


def bits_per_symbol(name: str) -> int:
    order = MODULATION_ORDER.get(name, 2)
    value = 0
    while order > 1:
        order //= 2
        value += 1
    return max(value, 1)
'''


METRICS_PY = r'''import math


def ber_probability(snr_db: float, bits_per_symbol: int, channel_factor: float) -> float:
    snr_linear = 10 ** (snr_db / 10.0)
    effective_snr = max(snr_linear * channel_factor / max(bits_per_symbol, 1), 1e-9)
    probability = 0.5 * math.exp(-0.75 * effective_snr)
    return min(max(probability, 1e-8), 0.5)
'''


SIMULATION_PY = r'''import numpy as np

from src.channel import channel_factor
from src.metrics import ber_probability
from src.modulation import bits_per_symbol


def run_simulation(config):
    rng = np.random.default_rng(int(config.get("seed", 42)))
    num_bits = int(config.get("num_bits", 4000))
    rows = []
    for modulation in config.get("modulations", ["BPSK"]):
        bps = bits_per_symbol(modulation)
        for channel in config.get("channels", ["AWGN"]):
            factor = channel_factor(channel)
            for snr_db in config.get("snr_db", [0, 5, 10, 15, 20]):
                probability = ber_probability(float(snr_db), bps, factor)
                bit_errors = int(rng.binomial(num_bits, probability))
                rows.append(
                    {
                        "snr_db": float(snr_db),
                        "modulation": modulation,
                        "channel": channel,
                        "bit_error_rate": bit_errors / num_bits,
                        "bit_errors": bit_errors,
                        "num_bits": num_bits,
                    }
                )
    return rows
'''
