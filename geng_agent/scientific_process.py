"""A scientific task entrypoint with non-blocking file observations."""
DRIVER = r"""
import sys, os, json, hashlib, importlib, importlib.machinery
_CONFIG = json.loads(sys.argv[1])
_ROOT = os.path.realpath(os.getcwd())
sys.path.insert(0, _ROOT)
_observation_stream = sys.stderr
_busy = False
_seen = set()
def _observe(event, args):
    global _busy
    if event != "open" or _busy or not args or not isinstance(args[0], (str, bytes)):
        return
    _busy = True
    try:
        path = os.path.realpath(os.fsdecode(args[0]))
        if os.path.commonpath((path, _ROOT)) != _ROOT:
            return
        relative = os.path.relpath(path, _ROOT).replace(chr(92), "/")
        if relative.startswith((".geng_", _CONFIG["task_output_prefix"])) or "__pycache__" in relative:
            return
        mode = args[1] if len(args) > 1 else "r"
        flags = args[2] if len(args) > 2 else 0
        writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))
        key = (relative, bool(writing))
        if key in _seen:
            return
        item = {"path": relative}
        if not writing:
            with open(path, "rb") as f:
                item["sha256"] = hashlib.file_digest(f, "sha256").hexdigest()
        _observation_stream.write(("GENG_OBSERVED_WRITE " if writing else "GENG_OBSERVED_READ ") + json.dumps(item) + "\n")
        _observation_stream.flush()
        _seen.add(key)
    except Exception:
        pass
    finally:
        _busy = False
sys.addaudithook(_observe)
# Execute local source rather than a timestamp-valid .pyc from an earlier run.
# This changes only loading; it never checks or vetoes the scientific program.
_get_code = importlib.machinery.SourceFileLoader.get_code
def _local_source_code(loader, fullname):
    path = os.path.realpath(loader.path)
    try:
        local = os.path.commonpath((path, _ROOT)) == _ROOT
    except ValueError:
        local = False
    if local:
        return loader.source_to_code(loader.get_data(loader.path), loader.path)
    return _get_code(loader, fullname)
importlib.machinery.SourceFileLoader.get_code = _local_source_code
_initial = set(sys.modules)
try:
    module = importlib.import_module("tasks." + _CONFIG["task_module"])
    result = module.main(_CONFIG["task_config"])
finally:
    try:
        _observation_stream.write("GENG_OBSERVED_MODULES " + json.dumps(sorted({name.split(".")[0] for name in set(sys.modules) - _initial})) + "\n")
        _observation_stream.flush()
    except Exception:
        pass
raise SystemExit(result if isinstance(result, int) and not isinstance(result, bool) else 0)
"""
