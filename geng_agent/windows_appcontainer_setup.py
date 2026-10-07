"""Inspect/install the narrowly scoped Windows volume-query capability.

Default invocation is read-only. --apply and --rollback require a terminal the
user has explicitly opened as Administrator; this tool never elevates itself.
It changes only this project's capability ACE, never Everyone/Users/package
permissions. Windows recreates these kernel objects at boot. The optional
tools/install_writer_boundary_startup.ps1 installer restores the same fixed
grants at startup; this inspection/apply command never installs a task itself.
"""
from __future__ import annotations

import argparse
import ctypes as c
from ctypes import wintypes as w
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import uuid

from . import windows_process_boundary as boundary


class UnicodeString(c.Structure):
    _fields_ = [("Length", w.USHORT), ("MaximumLength", w.USHORT), ("Buffer", w.LPWSTR)]


class ObjectAttributes(c.Structure):
    _fields_ = [("Length", w.ULONG), ("RootDirectory", w.HANDLE),
                ("ObjectName", c.POINTER(UnicodeString)), ("Attributes", w.ULONG),
                ("SecurityDescriptor", w.LPVOID), ("SecurityQualityOfService", w.LPVOID)]


def _apis():
    kernel, advapi = boundary._kernel, boundary._advapi
    native = c.WinDLL("ntdll", use_last_error=True)
    shell = c.WinDLL("shell32", use_last_error=True)
    boundary._bind(advapi, "GetSecurityInfo", [w.HANDLE, c.c_int, w.DWORD, w.LPVOID,
                    w.LPVOID, c.POINTER(w.LPVOID), w.LPVOID, c.POINTER(w.LPVOID)], w.DWORD)
    boundary._bind(advapi, "SetSecurityInfo", [w.HANDLE, c.c_int, w.DWORD, w.LPVOID,
                    w.LPVOID, w.LPVOID, w.LPVOID], w.DWORD)
    boundary._bind(advapi, "ConvertSecurityDescriptorToStringSecurityDescriptorW",
                    [w.LPVOID, w.DWORD, w.DWORD, c.POINTER(w.LPWSTR), w.LPVOID])
    boundary._bind(native, "NtOpenDirectoryObject", [c.POINTER(w.HANDLE), w.DWORD,
                    c.POINTER(ObjectAttributes)], c.c_long)
    boundary._bind(native, "NtOpenSymbolicLinkObject", [c.POINTER(w.HANDLE), w.DWORD,
                    c.POINTER(ObjectAttributes)], c.c_long)
    boundary._bind(kernel, "CreateFileW", [w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID,
                    w.DWORD, w.DWORD, w.HANDLE], w.HANDLE)
    boundary._bind(shell, "IsUserAnAdmin", [], w.BOOL)
    return kernel, advapi, native, shell


def _targets(drives):
    return [
        {"name": r"\GLOBAL??", "kind": "directory", "mask": 0x20003},
        *[{"name": rf"\GLOBAL??\{drive.upper().rstrip(':')}:" , "kind": "link", "mask": 0x20001}
          for drive in drives],
        {"name": r"\GLOBAL??\MountPointManager", "kind": "link", "mask": 0x20001},
        {"name": r"\\.\MountPointManager", "kind": "device", "mask": 0x120089},
    ]


def _open(target, write, kernel, native):
    access = 0x20000 | (0x40000 if write else 0)  # READ_CONTROL / WRITE_DAC
    if target["kind"] == "device":
        handle = kernel.CreateFileW(target["name"], access, 3, None, 3, 0, None)
        boundary._check(handle not in (None, c.c_void_p(-1).value), "Open mount-manager security")
        return handle
    name = target["name"]
    text = UnicodeString(len(name) * 2, len(name) * 2 + 2, name)
    attributes = ObjectAttributes(c.sizeof(ObjectAttributes), None, c.pointer(text), 0x40, None, None)
    handle = w.HANDLE()
    function = native.NtOpenDirectoryObject if target["kind"] == "directory" else native.NtOpenSymbolicLinkObject
    result = function(c.byref(handle), access, c.byref(attributes))
    if result < 0:
        raise OSError(f"Cannot open {name}: NTSTATUS {result & 0xFFFFFFFF:#x}")
    return handle


def _read(handle, sid, kernel, advapi):
    acl, descriptor = w.LPVOID(), w.LPVOID()
    boundary._status(advapi.GetSecurityInfo(handle, 6, 4, None, None,
                     c.byref(acl), None, c.byref(descriptor)), "Read object ACL")
    text = w.LPWSTR()
    try:
        boundary._check(advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW(
                        descriptor, 1, 4, c.byref(text), None), "Describe object ACL")
        entries = []
        count = c.c_ushort.from_address(acl.value + 4).value if acl else 0
        for index in range(count):
            ace = w.LPVOID()
            boundary._check(advapi.GetAce(acl, index, c.byref(ace)), "Read object ACE")
            kind = c.c_ubyte.from_address(ace.value).value
            if kind in (0, 1) and advapi.EqualSid(ace.value + 8, sid):
                entries.append({"kind": kind,
                                "flags": c.c_ubyte.from_address(ace.value + 1).value,
                                "mask": w.DWORD.from_address(ace.value + 4).value})
        return text.value, entries
    finally:
        if text:
            kernel.LocalFree(text)
        kernel.LocalFree(descriptor)


def _replace_own_entries(handle, sid, entries, kernel, advapi):
    """Re-read current ACL and replace only this capability's simple ACEs."""
    old, descriptor, revoked, merged = w.LPVOID(), w.LPVOID(), w.LPVOID(), w.LPVOID()
    boundary._status(advapi.GetSecurityInfo(handle, 6, 4, None, None,
                     c.byref(old), None, c.byref(descriptor)), "Read current object ACL")
    try:
        remove = boundary._ExplicitAccess(0, 4, 0, boundary._Trustee(None, 0, 0, 0, sid))
        boundary._status(advapi.SetEntriesInAclW(1, c.byref(remove), old,
                        c.byref(revoked)), "Remove own capability ACE")
        if entries:
            changes = (boundary._ExplicitAccess * len(entries))(*[
                boundary._ExplicitAccess(entry["mask"], 3 if entry["kind"] == 1 else 1,
                                         entry["flags"] & 0xF,
                                         boundary._Trustee(None, 0, 0, 0, sid))
                for entry in entries])
            boundary._status(advapi.SetEntriesInAclW(len(entries), changes, revoked,
                            c.byref(merged)), "Restore own capability entries")
        boundary._status(advapi.SetSecurityInfo(handle, 6, 4, None, None,
                        merged or revoked, None), "Apply capability ACL")
    finally:
        for pointer in (merged, revoked, descriptor):
            if pointer:
                kernel.LocalFree(pointer)


def check_namespace_access(drives=("C", "D")):
    """Read-only preflight for the host; never requests elevation or writes."""
    if os.name != "nt":
        return {"ready": False, "error": "Windows is required", "missing": []}
    kernel, advapi, native, _ = _apis()
    sid = boundary._readonly_capability()
    missing, errors = [], []
    for target in _targets(drives):
        handle = None
        try:
            handle = _open(target, False, kernel, native)
            _, entries = _read(handle, sid, kernel, advapi)
            allowed = denied = 0
            for entry in entries:
                if entry["kind"] == 0:
                    allowed |= entry["mask"]
                else:
                    denied |= entry["mask"]
            if allowed & target["mask"] != target["mask"] or denied & target["mask"]:
                missing.append(target["name"])
        except Exception as exc:
            missing.append(target["name"])
            errors.append(str(exc))
        finally:
            if handle:
                kernel.CloseHandle(handle)
    return {"ready": not missing, "missing": missing, "errors": errors,
            "reason": None if not missing else "writer_boundary_setup_required",
            "setup_script": str(Path(__file__).resolve().parents[1] / "tools" / "setup_writer_appcontainer_access.py")}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check", action="store_true", help="read-only inspection (default)")
    modes.add_argument("--apply", action="store_true", help="add only the project's read capability; requires Administrator")
    modes.add_argument("--rollback", type=Path, metavar="APPLY_RECORD", help="restore only this capability's original entries")
    parser.add_argument("--drive", action="append", help="drive letter to support; default C and D")
    parser.add_argument("--report", type=Path, help="JSON audit file beneath D:/geng-artifacts/reports")
    args = parser.parse_args(argv)
    if os.name != "nt":
        parser.error("This setup applies only to Windows")
    kernel, advapi, native, shell = _apis()
    report_root = Path(r"D:\geng-artifacts\reports").resolve()
    report_root.mkdir(parents=True, exist_ok=True)
    report = (args.report or report_root / f"writer-ac-volume-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}.json").resolve()
    if not report.is_relative_to(report_root):
        parser.error("Reports must stay under D:/geng-artifacts/reports")
    drives = args.drive or ["C", "D"]
    if any(len(item.rstrip(":")) != 1 or not item.rstrip(":").isalpha() for item in drives):
        parser.error("--drive must be a single drive letter")
    sid = boundary._readonly_capability()
    result = {"time": datetime.now().astimezone().isoformat(),
              "mode": "apply" if args.apply else "rollback" if args.rollback else "check",
              "capability_sid": boundary._sid_string(sid), "objects": [],
              "note": "Kernel grants may reset at boot. Optional fixed startup restore: tools/install_writer_boundary_startup.ps1. This command does not elevate or schedule itself."}
    handles = []
    exit_code = 1
    try:
        if (args.apply or args.rollback) and not shell.IsUserAnAdmin():
            raise PermissionError("Open a terminal as Administrator and rerun this explicit command. No permissions were changed.")
        targets = _targets(drives)
        restore = None
        if args.rollback:
            restore = json.loads(args.rollback.read_text(encoding="utf-8"))
            if restore.get("mode") != "apply" or restore.get("capability_sid") != result["capability_sid"]:
                raise ValueError("Rollback requires an apply record for this project's capability")
            allowed = {item["name"]: item for item in _targets(list("ABCDEFGHIJKLMNOPQRSTUVWXYZ"))}
            targets = [allowed[item["name"]] for item in restore["objects"] if item.get("changed")]
        # Open every requested object before applying any mutation.
        for target in targets:
            handle = _open(target, bool(args.apply or args.rollback), kernel, native)
            handles.append(handle)
            sddl, entries = _read(handle, sid, kernel, advapi)
            granted = 0
            denied = 0
            for entry in entries:
                if entry["kind"] == 0:
                    granted |= entry["mask"]
                else:
                    denied |= entry["mask"]
            result["objects"].append({**target, "before_sddl": sddl,
                "before_capability_entries": entries, "ready": granted & target["mask"] == target["mask"] and not denied & target["mask"],
                "changed": False})
        for handle, item in zip(handles, result["objects"]):
            if args.apply and not item["ready"]:
                if any(entry["kind"] == 1 for entry in item["before_capability_entries"]):
                    raise ValueError("An existing deny ACE for this capability requires manual review; no deny was removed")
                entries = item["before_capability_entries"] + [{"kind": 0, "flags": 0, "mask": item["mask"]}]
                # Persist the rollback state before changing this object.
                item["changed"] = True
                report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                _replace_own_entries(handle, sid, entries, kernel, advapi)
                item["ready"] = True
            elif restore is not None:
                original = next(entry for entry in restore["objects"] if entry["name"] == item["name"])
                _replace_own_entries(handle, sid, original["before_capability_entries"], kernel, advapi)
                item["changed"] = True
                item["restored"] = True
        result["success"] = bool(restore is not None or all(item["ready"] for item in result["objects"]))
        exit_code = 0 if result["success"] else 2
    except Exception as exc:
        result["success"] = False
        result["error"] = str(exc)
    finally:
        for handle in handles:
            kernel.CloseHandle(handle)
        report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"mode": result["mode"], "success": result["success"],
                      "missing": [item["name"] for item in result["objects"] if not item["ready"]],
                      "error": result.get("error"), "report": str(report)}, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
