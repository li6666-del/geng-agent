"""Trusted reproduction-project IO runtime, injected verbatim into every project.

`IO_RUNTIME_PY` is written to ``src/_io.py`` of every generated reproduction
project. Generated task scripts MUST call its helpers (``begin`` / ``write_table``
/ ``write_figure`` / ``finish``) for ALL artifact writing instead of hand-rolling
CSV / JSON / figure serialization and self-checks. It is NOT produced by the model
and MUST NOT be edited during repair -- it is the deterministic ``p≈1`` layer that
guarantees the artifact-correctness rules the model used to re-derive (and get
wrong) on every file.

`BACKEND_RUNTIME_PY` is the same idea for optional compute backends. It owns the
fragile "is torch importable / is CUDA available" probing so generated code does
not need forbidden ``importlib`` checks or broad try/except import guards.

The string is authored once, here, and is identical for every paper because it
only does paper-independent plumbing (seed, write CSV/PNG/summary, scrub
NaN/Inf/complex/numpy, self-check, honest exit code). It is deliberately written
to pass this project's own ``static_scan_repro_project`` (no forbidden imports,
calls, dynamic builtins, env access, or absolute-path literals)."""
from __future__ import annotations

import re
from pathlib import Path


def io_slug(value: object) -> str:
    """Filesystem-safe slug for a task id, IDENTICAL to ``_slug`` inside IO_RUNTIME_PY.

    The harness uses this to know which ``outputs/<slug>/`` folder ``_io`` will write a
    task's artifacts into, so the per-task artifact gate looks in the right place. Kept
    byte-for-byte in sync with the runtime by ``test_io_runtime`` (any drift fails)."""
    text = str(value).strip() or "task"
    safe = "".join(ch if (ch.isalnum() or ch in "._-") else "_" for ch in text)
    return safe.strip("._-") or "task"


# ---------------------------------------------------------------------------
# The verbatim content of src/_io.py inside every generated project.
# ---------------------------------------------------------------------------
IO_RUNTIME_PY = r'''"""Trusted IO runtime for this reproduction project (injected by geng-agent).

DO NOT EDIT. Generated task scripts import and call these helpers for every
artifact they write. They make the artifact-correctness guarantees deterministic
so generated code cannot get them wrong:

- begin(task_id, config) -> seeds numpy + random from config["seed"], records the
  seed, creates outputs/<task_id>/, and returns a numpy Generator.
- write_table(task_id, columns, rows) -> outputs/<task_id>/results.csv with a
  header and >=1 row; every cell coerced to a finite real scalar (complex -> real
  part, arrays -> mean, NaN/Inf -> blank).
- write_figure(task_id, name, fig) -> outputs/<task_id>/<name>.png; refuses to
  save an empty figure.
- finish(task_id, metrics, assumptions) -> outputs/<task_id>/summary.json, coerced
  to JSON-safe builtin types (numpy -> builtin, NaN/Inf -> null), re-read to
  self-check, returning an honest exit code (0 only on success).
"""
from __future__ import annotations

import csv
import json
import math
import random
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

__all__ = [
    "begin",
    "write_table",
    "write_figure",
    "finish",
    "outputs_dir",
]

_OUTPUTS = Path("outputs")
_SEEDS = {}


def _slug(value):
    text = str(value).strip() or "task"
    safe = "".join(ch if (ch.isalnum() or ch in "._-") else "_" for ch in text)
    return safe.strip("._-") or "task"


def outputs_dir(task_id):
    path = _OUTPUTS / _slug(task_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _to_int(value, default):
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def begin(task_id, config):
    """Seed RNGs deterministically, create outputs/<task_id>/, return a Generator."""
    seed = 12345
    if isinstance(config, dict):
        seed = _to_int(config.get("seed"), 12345)
    np.random.seed(seed)
    random.seed(seed)
    _SEEDS[_slug(task_id)] = seed
    outputs_dir(task_id)
    return np.random.default_rng(seed)


def _real_scalar(value):
    """Best-effort convert any value to a finite Python float, else None."""
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, complex):
        number = float(value.real)
        return number if math.isfinite(number) else None
    if isinstance(value, np.generic):
        return _real_scalar(value.item())
    if isinstance(value, np.ndarray):
        flat = np.asarray(value).reshape(-1)
        if flat.size == 0:
            return None
        return _real_scalar(flat.mean())
    return None


def _cell(value):
    """A CSV cell: real strings pass through, numbers -> finite real text, else blank."""
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    number = _real_scalar(value)
    return repr(number) if number is not None else ""


def write_table(task_id, columns, rows):
    """Write outputs/<task_id>/results.csv with a header and >=1 coerced data row."""
    columns = [str(col) for col in (columns or [])]
    if not columns:
        raise ValueError("write_table requires at least one column name")
    norm_rows = []
    for row in rows or []:
        if isinstance(row, dict):
            norm_rows.append([_cell(row.get(col)) for col in columns])
        elif isinstance(row, (list, tuple)):
            values = list(row) + [""] * (len(columns) - len(row))
            norm_rows.append([_cell(value) for value in values[: len(columns)]])
        else:
            norm_rows.append([_cell(row)] + [""] * (len(columns) - 1))
    if not norm_rows:
        raise ValueError("write_table requires at least one data row")
    path = outputs_dir(task_id) / "results.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        writer.writerows(norm_rows)
    with path.open("r", encoding="utf-8", newline="") as handle:
        check = list(csv.reader(handle))
    if len(check) < 2:
        raise ValueError("results.csv self-check failed: missing data rows")
    return str(path)


def _figure_has_content(fig):
    for axes in fig.get_axes():
        if axes.lines or axes.patches or axes.collections or axes.images:
            return True
    return False


def write_figure(task_id, name, fig=None):
    """Save a non-empty matplotlib figure to outputs/<task_id>/<name>.png, then close it."""
    figure = fig if fig is not None else plt.gcf()
    if not _figure_has_content(figure):
        plt.close(figure)
        raise ValueError("write_figure refused to save an empty figure")
    stem = _slug(name)
    if stem.lower().endswith(".png"):
        stem = stem[:-4] or "figure"
    path = outputs_dir(task_id) / (stem + ".png")
    figure.savefig(path, dpi=150)
    plt.close(figure)
    return str(path)


def _json_safe(value):
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str) or value is None:
        return value
    if isinstance(value, complex):
        number = float(value.real)
        return number if math.isfinite(number) else None
    if isinstance(value, np.generic):
        return _json_safe(value.item())
    if isinstance(value, np.ndarray):
        return _json_safe(value.tolist())
    return str(value)


def finish(task_id, metrics=None, assumptions=None, extra=None, ok=True):
    """Write outputs/<task_id>/summary.json (JSON-safe + self-checked). Return exit code."""
    metrics_safe = _json_safe(metrics if metrics is not None else {})
    if not isinstance(metrics_safe, (dict, list)):
        metrics_safe = {"value": metrics_safe}
    assumptions_safe = _json_safe(assumptions if assumptions is not None else [])
    if not isinstance(assumptions_safe, list):
        assumptions_safe = [assumptions_safe]
    summary = {
        "task_id": str(task_id),
        "seed": _SEEDS.get(_slug(task_id)),
        "ok": bool(ok),
        "metrics": metrics_safe,
        "assumptions": assumptions_safe,
    }
    if isinstance(extra, dict):
        for key, item in extra.items():
            summary[str(key)] = _json_safe(item)
    path = outputs_dir(task_id) / "summary.json"
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    reread = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(reread, dict) or "task_id" not in reread:
        raise ValueError("summary.json self-check failed")
    return 0 if ok else 1
'''


# A short, static description of the runtime API, embedded into the codegen prompts
# so the model knows how to CALL src/_io.py without ever seeing or editing its body.
IO_RUNTIME_API_DOC = """\
src/_io.py 是本地已提供的“受信任运行时”，已经在项目里，禁止生成或修改它，只能 `from src import _io` 调用。
所有 CSV / PNG / summary.json 必须通过它写出，不要自己写 csv/json/savefig 的序列化或写后自检逻辑：
- `rng = _io.begin(task_id, config)`：按 config["seed"] 播种 numpy+random、建 outputs/<task_id>/、返回 numpy Generator。
  在任务 main() 里应尽早调用它；smoke 和 full 都可由 writer 直接运行。
- `_io.write_table(task_id, columns, rows)`：写 outputs/<task_id>/results.csv（带表头、≥1 行；每格自动转有限实数，复数取实部、数组取均值、NaN/Inf 留空）。rows 可为 list[dict] 或 list[list]。
- `_io.write_figure(task_id, name, fig)`：把非空 matplotlib figure 存成 outputs/<task_id>/<name>.png（空图会报错）。
- `return _io.finish(task_id, metrics=..., assumptions=...)`：写 outputs/<task_id>/summary.json（自动转 JSON 安全类型、刷 NaN/Inf、写后复读自检）并返回诚实退出码；放在 main() 末尾 `raise SystemExit(_io.finish(...))`。
"""


BACKEND_RUNTIME_PY = r'''"""Trusted optional compute-backend runtime (injected by geng-agent).

DO NOT EDIT. Generated code may call this helper to decide whether a torch/CUDA
backend is actually available. Keeping the probe here prevents generated science
files from using importlib or broad try/except around third-party imports.
"""
from __future__ import annotations

_TORCH = None
_TORCH_PROBED = False
_TORCH_ERROR = ""

__all__ = ["select_backend", "torch", "torch_available"]


def _as_int(value, default=0):
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _load_torch():
    global _TORCH, _TORCH_PROBED, _TORCH_ERROR
    if _TORCH_PROBED:
        return _TORCH
    _TORCH_PROBED = True
    try:
        import torch as torch_module
    except Exception as exc:  # trusted runtime records the reason instead of hiding it
        _TORCH = None
        _TORCH_ERROR = f"{type(exc).__name__}: {exc}"
    else:
        _TORCH = torch_module
        _TORCH_ERROR = ""
    return _TORCH


def torch_available(require_cuda=False):
    module = _load_torch()
    if module is None:
        return False
    if require_cuda:
        try:
            return bool(module.cuda.is_available())
        except Exception:
            return False
    return True


def torch():
    module = _load_torch()
    if module is None:
        raise RuntimeError("torch is not importable in this interpreter: " + (_TORCH_ERROR or "unknown error"))
    return module


def select_backend(config=None, work_items=0, heavy=False):
    cfg = config if isinstance(config, dict) else {}
    prefer = str(cfg.get("backend") or cfg.get("compute_backend") or "auto").strip().lower()
    min_cuda_work_items = _as_int(cfg.get("min_cuda_work_items"), 0)
    work = _as_int(work_items, 0)
    info = {
        "backend": "numpy",
        "requested": prefer,
        "fallback_reason": "",
        "torch_available": False,
        "cuda_available": False,
    }

    if prefer in {"numpy", "cpu", "numpy_cpu"}:
        info["fallback_reason"] = "cuda_not_requested"
        return info

    if prefer not in {"auto", "cuda", "torch_cuda", "torch", "torch_cpu"}:
        info["fallback_reason"] = "unsupported_backend_request"
        return info

    module = _load_torch()
    if module is None:
        info["fallback_reason"] = "torch_not_importable:" + (_TORCH_ERROR or "unknown")
        return info

    info["torch_available"] = True
    try:
        info["torch_version"] = str(module.__version__)
    except Exception:
        pass

    try:
        cuda_ok = bool(module.cuda.is_available())
    except Exception as exc:
        cuda_ok = False
        info["cuda_probe_error"] = f"{type(exc).__name__}: {exc}"
    info["cuda_available"] = cuda_ok

    if prefer in {"torch", "torch_cpu"}:
        info["backend"] = "torch_cpu"
        return info

    if prefer == "auto" and not heavy and work < min_cuda_work_items:
        info["fallback_reason"] = "workload_below_cuda_threshold"
        return info

    if cuda_ok:
        info["backend"] = "torch_cuda"
        try:
            info["cuda_device"] = str(module.cuda.get_device_name(0))
        except Exception:
            pass
        return info

    info["fallback_reason"] = "torch_importable_but_cuda_unavailable"
    return info
'''


BACKEND_RUNTIME_API_DOC = """\
src/_backend.py 是本地已提供的“受信任计算后端探测器”，已经在项目里，禁止生成或修改它。
生成代码不要自己 import importlib，也不要用 broad try/except 包住 `import torch` 来探测 GPU。
- `from src import _backend`
- `backend = _backend.select_backend(config, work_items=..., heavy=True)`：返回 `backend/fallback_reason/torch_available/cuda_available/torch_version/cuda_device` 等信息。
- 当 `backend["backend"] == "torch_cuda"` 或 `"torch_cpu"` 时，可用 `torch = _backend.torch()` 取得 torch 模块并实现批量化计算。
- `select_backend()` 只负责探测和选择，不会自动把 NumPy 计算搬到 GPU。选择 `torch_cuda` 后，核心 Monte Carlo、矩阵和批量数值计算必须真实使用 CUDA tensor；只把 backend 字典写进 summary 不算使用 GPU。
- full 前应先探测 CUDA，并根据任务规模、可向量化程度和预计耗时选择后端。若 CUDA 可用但仍选择 CPU，必须在结果中说明 CPU 更合适的具体原因。
- 运行结果必须记录实际计算设备证据和 full 耗时；不能把 `requested=cuda`、`cuda_available=true` 或设备名称当成已经在 GPU 上完成计算的证据。
- 如果调用 `_backend.torch()` 或实际使用 torch 后端，requirements.txt 必须包含 `torch`；若只是 CPU/NumPy fallback，可不写 torch。
- summary.json 的 metrics 里必须写入这个 backend 字典，不能静默降级后假装用了 GPU。
"""


def inject_io_runtime(project_dir: Path) -> Path:
    """Write the trusted src/_io.py / src/_backend.py (and src/__init__.py) into a project, and make
    sure numpy + matplotlib are declared so its imports pass the consistency gate.
    Idempotent: safe to call on a fresh project or an existing/candidate copy."""
    project_dir = Path(project_dir)
    src_dir = project_dir / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    (src_dir / "_io.py").write_text(IO_RUNTIME_PY, encoding="utf-8", newline="\n")
    (src_dir / "_backend.py").write_text(BACKEND_RUNTIME_PY, encoding="utf-8", newline="\n")
    init_path = src_dir / "__init__.py"
    if not init_path.exists():
        init_path.write_text("", encoding="utf-8", newline="\n")
    ensure_runtime_requirements(project_dir)
    return src_dir / "_io.py"


def ensure_runtime_requirements(project_dir: Path) -> list[str]:
    """Guarantee numpy + matplotlib (which src/_io.py imports) are in requirements.txt.
    Returns the package names that had to be added (empty if nothing changed)."""
    req_path = Path(project_dir) / "requirements.txt"
    needed = ["numpy", "matplotlib"]
    existing_lines: list[str] = []
    declared: set[str] = set()
    if req_path.exists():
        for raw in req_path.read_text(encoding="utf-8", errors="replace").splitlines():
            existing_lines.append(raw)
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            package = re.split(r"[<>=!~\[]", line, maxsplit=1)[0].strip().lower().replace("_", "-")
            declared.add(package)
    additions = [package for package in needed if package not in declared]
    if not additions:
        return []
    lines = [line for line in existing_lines if line.strip()]
    lines.extend(additions)
    req_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return additions
