"""Per-Writer Windows AppContainers, including every native child process.

The boundary is enforced by Windows tokens, not command text or agent output.
An AppContainer can manage its own processes but cannot open another container's
or the host's processes for termination/injection. There is no ordinary-Popen
fallback. The Windows user need not be an administrator.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes as wt
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import threading
import uuid
from contextlib import contextmanager

_LOG = logging.getLogger(__name__)
_ACL_LOCK = threading.RLock()
_READ_CAPABILITY = None


class ProcessBoundaryUnavailable(RuntimeError):
    """The requested OS boundary could not be established."""


if os.name == "nt":
    import _winapi

    _kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    _advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    _userenv = ctypes.WinDLL("userenv", use_last_error=True)
    _base = ctypes.WinDLL("kernelbase", use_last_error=True)

    class _SidAttributes(ctypes.Structure):
        _fields_ = [("Sid", wt.LPVOID), ("Attributes", wt.DWORD)]

    class _Capabilities(ctypes.Structure):
        _fields_ = [("AppContainerSid", wt.LPVOID),
                    ("Capabilities", ctypes.POINTER(_SidAttributes)),
                    ("CapabilityCount", wt.DWORD), ("Reserved", wt.DWORD)]

    class _StartupInfo(ctypes.Structure):
        _fields_ = [("cb", wt.DWORD), ("lpReserved", wt.LPWSTR),
                    ("lpDesktop", wt.LPWSTR), ("lpTitle", wt.LPWSTR),
                    ("dwX", wt.DWORD), ("dwY", wt.DWORD),
                    ("dwXSize", wt.DWORD), ("dwYSize", wt.DWORD),
                    ("dwXCountChars", wt.DWORD), ("dwYCountChars", wt.DWORD),
                    ("dwFillAttribute", wt.DWORD), ("dwFlags", wt.DWORD),
                    ("wShowWindow", wt.WORD), ("cbReserved2", wt.WORD),
                    ("lpReserved2", wt.LPVOID), ("hStdInput", wt.HANDLE),
                    ("hStdOutput", wt.HANDLE), ("hStdError", wt.HANDLE)]

    class _StartupInfoEx(ctypes.Structure):
        _fields_ = [("StartupInfo", _StartupInfo), ("lpAttributeList", wt.LPVOID)]

    class _ProcessInfo(ctypes.Structure):
        _fields_ = [("hProcess", wt.HANDLE), ("hThread", wt.HANDLE),
                    ("dwProcessId", wt.DWORD), ("dwThreadId", wt.DWORD)]

    class _AttributeTag(ctypes.Structure):
        _fields_ = [("FileAttributes", wt.DWORD), ("ReparseTag", wt.DWORD)]

    class _Trustee(ctypes.Structure):
        _fields_ = [("pMultipleTrustee", wt.LPVOID), ("MultipleTrusteeOperation", ctypes.c_int),
                    ("TrusteeForm", ctypes.c_int), ("TrusteeType", ctypes.c_int),
                    ("ptstrName", wt.LPVOID)]

    class _ExplicitAccess(ctypes.Structure):
        _fields_ = [("grfAccessPermissions", wt.DWORD), ("grfAccessMode", ctypes.c_int),
                    ("grfInheritance", wt.DWORD), ("Trustee", _Trustee)]

    class _BasicJobLimit(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                    ("PerJobUserTimeLimit", ctypes.c_longlong), ("LimitFlags", wt.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wt.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wt.DWORD),
                    ("SchedulingClass", wt.DWORD)]

    class _IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in
                    ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                     "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _JobLimit(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _BasicJobLimit), ("IoInfo", _IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    def _bind(dll, name, args, result=wt.BOOL):
        func = getattr(dll, name)
        func.argtypes, func.restype = args, result
        return func

    _bind(_kernel, "CloseHandle", [wt.HANDLE])
    _bind(_kernel, "GetCurrentProcess", [], wt.HANDLE)
    _bind(_kernel, "CreateMutexW", [wt.LPVOID, wt.BOOL, wt.LPCWSTR], wt.HANDLE)
    _bind(_kernel, "WaitForSingleObject", [wt.HANDLE, wt.DWORD], wt.DWORD)
    _bind(_kernel, "ReleaseMutex", [wt.HANDLE])
    _bind(_kernel, "CreateFileW", [wt.LPCWSTR, wt.DWORD, wt.DWORD, wt.LPVOID,
          wt.DWORD, wt.DWORD, wt.HANDLE], wt.HANDLE)
    _bind(_kernel, "GetFileInformationByHandleEx", [wt.HANDLE, ctypes.c_int, wt.LPVOID, wt.DWORD])
    _bind(_kernel, "LocalFree", [wt.LPVOID], wt.LPVOID)
    _bind(_kernel, "CreateProcessW", [wt.LPCWSTR, wt.LPWSTR, wt.LPVOID, wt.LPVOID,
          wt.BOOL, wt.DWORD, wt.LPVOID, wt.LPCWSTR, ctypes.POINTER(_StartupInfoEx), ctypes.POINTER(_ProcessInfo)])
    _bind(_kernel, "InitializeProcThreadAttributeList", [wt.LPVOID, wt.DWORD, wt.DWORD, ctypes.POINTER(ctypes.c_size_t)])
    _bind(_kernel, "UpdateProcThreadAttribute", [wt.LPVOID, wt.DWORD, ctypes.c_size_t, wt.LPVOID, ctypes.c_size_t, wt.LPVOID, wt.LPVOID])
    _bind(_kernel, "DeleteProcThreadAttributeList", [wt.LPVOID], None)
    _bind(_kernel, "CreateJobObjectW", [wt.LPVOID, wt.LPCWSTR], wt.HANDLE)
    _bind(_kernel, "SetInformationJobObject", [wt.HANDLE, ctypes.c_int, wt.LPVOID, wt.DWORD])
    _bind(_kernel, "AssignProcessToJobObject", [wt.HANDLE, wt.HANDLE])
    _bind(_kernel, "TerminateJobObject", [wt.HANDLE, wt.UINT])
    _bind(_kernel, "TerminateProcess", [wt.HANDLE, wt.UINT])
    _bind(_kernel, "ResumeThread", [wt.HANDLE], wt.DWORD)
    _bind(_advapi, "OpenProcessToken", [wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.HANDLE)])
    _bind(_advapi, "GetTokenInformation", [wt.HANDLE, ctypes.c_int, wt.LPVOID, wt.DWORD, ctypes.POINTER(wt.DWORD)])
    _bind(_advapi, "EqualSid", [wt.LPVOID, wt.LPVOID])
    _bind(_advapi, "FreeSid", [wt.LPVOID], wt.LPVOID)
    _bind(_advapi, "ConvertStringSidToSidW", [wt.LPCWSTR, ctypes.POINTER(wt.LPVOID)])
    _bind(_advapi, "ConvertSidToStringSidW", [wt.LPVOID, ctypes.POINTER(wt.LPWSTR)])
    _bind(_advapi, "GetNamedSecurityInfoW", [wt.LPCWSTR, ctypes.c_int, wt.DWORD, wt.LPVOID, wt.LPVOID,
          ctypes.POINTER(wt.LPVOID), wt.LPVOID, ctypes.POINTER(wt.LPVOID)], wt.DWORD)
    _bind(_advapi, "SetEntriesInAclW", [wt.DWORD, ctypes.POINTER(_ExplicitAccess), wt.LPVOID, ctypes.POINTER(wt.LPVOID)], wt.DWORD)
    _bind(_advapi, "SetNamedSecurityInfoW", [wt.LPWSTR, ctypes.c_int, wt.DWORD, wt.LPVOID, wt.LPVOID, wt.LPVOID, wt.LPVOID], wt.DWORD)
    _bind(_advapi, "GetSecurityInfo", [wt.HANDLE, ctypes.c_int, wt.DWORD, wt.LPVOID, wt.LPVOID,
          ctypes.POINTER(wt.LPVOID), wt.LPVOID, ctypes.POINTER(wt.LPVOID)], wt.DWORD)
    _bind(_advapi, "SetSecurityInfo", [wt.HANDLE, ctypes.c_int, wt.DWORD, wt.LPVOID, wt.LPVOID,
          wt.LPVOID, wt.LPVOID], wt.DWORD)
    _native = ctypes.WinDLL("ntdll", use_last_error=True)
    _bind(_native, "NtSetSecurityObject", [wt.HANDLE, wt.DWORD, wt.LPVOID], ctypes.c_long)
    _bind(_advapi, "GetAce", [wt.LPVOID, wt.DWORD, ctypes.POINTER(wt.LPVOID)])
    _bind(_base, "DeriveCapabilitySidsFromName", [wt.LPCWSTR, ctypes.POINTER(wt.LPVOID),
          ctypes.POINTER(wt.DWORD), ctypes.POINTER(wt.LPVOID), ctypes.POINTER(wt.DWORD)], ctypes.c_long)
    _bind(_advapi, "ConvertStringSecurityDescriptorToSecurityDescriptorW", [wt.LPCWSTR, wt.DWORD, ctypes.POINTER(wt.LPVOID), wt.LPVOID])
    _bind(_advapi, "GetSecurityDescriptorSacl", [wt.LPVOID, ctypes.POINTER(wt.BOOL), ctypes.POINTER(wt.LPVOID), ctypes.POINTER(wt.BOOL)])
    _bind(_advapi, "InitializeSecurityDescriptor", [wt.LPVOID, wt.DWORD])
    _bind(_advapi, "SetSecurityDescriptorDacl", [wt.LPVOID, wt.BOOL, wt.LPVOID, wt.BOOL])
    _bind(_advapi, "SetFileSecurityW", [wt.LPCWSTR, wt.DWORD, wt.LPVOID])
    _bind(_userenv, "CreateAppContainerProfile", [wt.LPCWSTR, wt.LPCWSTR, wt.LPCWSTR, wt.LPVOID, wt.DWORD, ctypes.POINTER(wt.LPVOID)], ctypes.c_long)
    _bind(_userenv, "DeleteAppContainerProfile", [wt.LPCWSTR], ctypes.c_long)


def _check(ok, operation):
    if not ok:
        raise ProcessBoundaryUnavailable(f"{operation}: {ctypes.WinError(ctypes.get_last_error())}")


def _status(code, operation):
    if code:
        raise ProcessBoundaryUnavailable(f"{operation}: Windows error {code:#x}")


def _sid_string(sid):
    text = wt.LPWSTR()
    _check(_advapi.ConvertSidToStringSidW(sid, ctypes.byref(text)), "ConvertSidToStringSid")
    try:
        return text.value
    finally:
        _kernel.LocalFree(text)


@contextmanager
def _acl_guard():
    # Different worker/probe processes may share the same Python runtime.
    # Serialize read/merge/write so one domain cannot erase another's ACE.
    with _ACL_LOCK:
        mutex = _kernel.CreateMutexW(None, False, "Local\\GengWriterAppContainerAcl")
        _check(mutex, "Open runtime permission lock")
        try:
            result = _kernel.WaitForSingleObject(mutex, 120000)
            if result not in (0, 0x80):
                raise ProcessBoundaryUnavailable("Timed out acquiring runtime permission lock")
            try:
                yield
            finally:
                _kernel.ReleaseMutex(mutex)
        finally:
            _kernel.CloseHandle(mutex)


def _safe_absolute(path, *, strict=True):
    candidate = Path(os.path.abspath(os.fspath(path)))
    for item in (*reversed(candidate.parents), candidate):
        try:
            attributes = item.lstat().st_file_attributes
        except FileNotFoundError:
            if strict:
                raise
            continue
        if attributes & 0x400:
            raise ProcessBoundaryUnavailable(f"An authorization path contains a reparse point: {item}")
    return candidate


@contextmanager
def _fixed_authorization_path(path, *, access=0x60080):
    """Hold every directory against rename/junction substitution during ACL IO."""
    handles = []
    try:
        for item in (*reversed(path.parents), path):
            requested_access = access if item == path else 0x20080
            handle = _kernel.CreateFileW(str(item), requested_access,
                                         3, None, 3, 0x02200000, None)
            _check(handle not in (None, ctypes.c_void_p(-1).value),
                   f"Lock authorization path {item} (access={requested_access:#x})")
            handles.append(handle)
            info = _AttributeTag()
            _check(_kernel.GetFileInformationByHandleEx(handle, 9, ctypes.byref(info),
                                                        ctypes.sizeof(info)), f"Inspect authorization path {item}")
            if info.FileAttributes & 0x400:
                raise ProcessBoundaryUnavailable(f"An authorization path contains a reparse point: {item}")
        yield handles[-1]
    finally:
        for handle in reversed(handles):
            _kernel.CloseHandle(handle)


def _modify_acl(path, sid, mask, *, revoke=False, inherit=True):
    """Merge/remove this domain's ACE only; preserve all unrelated permissions."""
    old_acl, old_sd, new_acl = wt.LPVOID(), wt.LPVOID(), wt.LPVOID()
    with _acl_guard(), _fixed_authorization_path(path) as handle:
        _status(_advapi.GetSecurityInfo(handle, 1, 4, None, None,
                ctypes.byref(old_acl), None, ctypes.byref(old_sd)), "Read runtime ACL")
        try:
            entry = _ExplicitAccess(mask, 4 if revoke else 1, 3 if path.is_dir() and inherit else 0,
                                    _Trustee(None, 0, 0, 0, sid))
            _status(_advapi.SetEntriesInAclW(1, ctypes.byref(entry), old_acl,
                                          ctypes.byref(new_acl)), "Merge runtime ACL")
            if inherit:
                _status(_advapi.SetSecurityInfo(handle, 1, 4, None, None,
                                                    new_acl, None), "Apply runtime ACL")
            else:
                # Unlike SetNamedSecurityInfo, this does not walk a potentially
                # enormous ancestor subtree to propagate inherited ACEs. The
                # metadata grant deliberately affects this directory only.
                descriptor = ctypes.create_string_buffer(64)
                _check(_advapi.InitializeSecurityDescriptor(descriptor, 1), "Initialize directory ACL")
                _check(_advapi.SetSecurityDescriptorDacl(descriptor, True, new_acl, False), "Set directory ACL")
                _status(_native.NtSetSecurityObject(handle, 4, descriptor), "Apply directory-only ACL")
        finally:
            if new_acl:
                _kernel.LocalFree(new_acl)
            if old_sd:
                _kernel.LocalFree(old_sd)


def _readonly_capability():
    """Stable, read-only software capability; never used for private case data."""
    global _READ_CAPABILITY
    with _ACL_LOCK:
        if _READ_CAPABILITY is not None:
            return _READ_CAPABILITY
        groups, capabilities = wt.LPVOID(), wt.LPVOID()
        group_count, count = wt.DWORD(), wt.DWORD()
        _check(_base.DeriveCapabilitySidsFromName("GengAgent.TrustedRuntime.Read.v1",
                ctypes.byref(groups), ctypes.byref(group_count),
                ctypes.byref(capabilities), ctypes.byref(count)), "Derive software read capability")
        try:
            if not count.value:
                raise ProcessBoundaryUnavailable("Windows returned no runtime capability SID")
            text = _sid_string(ctypes.cast(capabilities, ctypes.POINTER(wt.LPVOID))[0])
            _READ_CAPABILITY = wt.LPVOID()
            _check(_advapi.ConvertStringSidToSidW(text, ctypes.byref(_READ_CAPABILITY)), "Copy software capability")
            return _READ_CAPABILITY
        finally:
            for array, length in ((groups, group_count.value), (capabilities, count.value)):
                for index in range(length):
                    _kernel.LocalFree(ctypes.cast(array, ctypes.POINTER(wt.LPVOID))[index])
                if array:
                    _kernel.LocalFree(array)


def _ensure_shared_runtime_read(path):
    sid = _readonly_capability()
    with _acl_guard():
        acl, descriptor = wt.LPVOID(), wt.LPVOID()
        _status(_advapi.GetNamedSecurityInfoW(str(path), 1, 4, None, None,
                ctypes.byref(acl), None, ctypes.byref(descriptor)), "Inspect software ACL")
        try:
            # ACL header: revision/flags/size/ACE count/reserved.
            count = ctypes.c_ushort.from_address(acl.value + 4).value if acl else 0
            for index in range(count):
                ace = wt.LPVOID()
                _check(_advapi.GetAce(acl, index, ctypes.byref(ace)), "Inspect software ACE")
                kind = ctypes.c_ubyte.from_address(ace.value).value
                flags = ctypes.c_ubyte.from_address(ace.value + 1).value
                mask = wt.DWORD.from_address(ace.value + 4).value
                if (kind == 0 and mask & 0x1200A9 == 0x1200A9
                        and (not path.is_dir() or flags & 3 == 3)
                        and _advapi.EqualSid(ace.value + 8, sid)):
                    return
        finally:
            _kernel.LocalFree(descriptor)
        _modify_acl(path, sid, 0x1200A9)


def _allow_low_integrity_write(path):
    # Only private Writer paths receive this label, never shared read-only code.
    descriptor, sacl = wt.LPVOID(), wt.LPVOID()
    present, defaulted = wt.BOOL(), wt.BOOL()
    _check(_advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
           "S:(ML;OICI;NW;;;LW)", 1, ctypes.byref(descriptor), None), "Build private directory label")
    try:
        _check(_advapi.GetSecurityDescriptorSacl(descriptor, ctypes.byref(present),
               ctypes.byref(sacl), ctypes.byref(defaulted)), "Read private directory label")
        with _fixed_authorization_path(path, access=0xA0080) as handle:
            _status(_advapi.SetSecurityInfo(handle, 1, 0x10, None, None,
                                            None, sacl), "Label private directory")
    finally:
        _kernel.LocalFree(descriptor)


def _token_data(token, kind):
    size = wt.DWORD()
    _advapi.GetTokenInformation(token, kind, None, 0, ctypes.byref(size))
    buffer = ctypes.create_string_buffer(size.value)
    _check(_advapi.GetTokenInformation(token, kind, buffer, size.value,
                                     ctypes.byref(size)), "Inspect child security token")
    return buffer


class WriterProcessDomain:
    """One identity for a Writer and its experiments; siblings have other SIDs."""

    def __init__(self, workspace: Path, extra_write_roots=()):
        if os.name != "nt":
            raise ProcessBoundaryUnavailable("WriterProcessDomain requires Windows AppContainer support")
        self.workspace = _safe_absolute(workspace)
        self.profile = "GengWriter." + uuid.uuid4().hex
        self._sid = wt.LPVOID()
        self._grants = {}
        self._runtime_roots = set()
        self._ancestor_grants = set()
        self._processes = []
        self._closed = False
        self._lock = threading.RLock()
        host_token = wt.HANDLE()
        _check(_advapi.OpenProcessToken(_kernel.GetCurrentProcess(), 8, ctypes.byref(host_token)), "Read host identity")
        try:
            self._host_user = _token_data(host_token, 1)
            self._host_sid = ctypes.cast(self._host_user, ctypes.POINTER(_SidAttributes)).contents.Sid
        finally:
            _kernel.CloseHandle(host_token)
        _status(_userenv.CreateAppContainerProfile(self.profile, self.profile,
                "Isolated Geng Agent Writer", None, 0, ctypes.byref(self._sid)), "Create Writer AppContainer")
        self.sid = _sid_string(self._sid)
        try:
            self.allow_write(self.workspace)
            self.private_home = self.workspace / ".geng_process_home"
            self.private_temp = self.private_home / "tmp"
            for path in (self.private_home, self.private_temp):
                path.mkdir(parents=True, exist_ok=True)
            for path in extra_write_roots:
                self.allow_write(path)
            self.allow_runtime_read(Path(sys.base_prefix))
            self.allow_runtime_read(Path(sys.prefix))
        except BaseException:
            self.close()
            raise

    @property
    def policy(self):
        return {"provider": "windows_appcontainer", "process_isolation": True,
                "appcontainer_sid": self.sid, "host_process_access": "denied",
                "sibling_writer_process_access": "denied"}

    def check_codex_compatibility(self):
        from .windows_appcontainer_setup import check_namespace_access
        return check_namespace_access()

    def allow_read(self, path):
        path = _safe_absolute(path)
        with self._lock:
            if self._closed:
                raise ProcessBoundaryUnavailable("Writer process domain is closed")
            if any(path == root or (root.is_dir() and path.is_relative_to(root))
                   for root in self._runtime_roots):
                return
        self._grant(path, 0x1200A9)

    def allow_runtime_read(self, path):
        """Authorize trusted installed software, not project inputs or secrets."""
        path = _safe_absolute(path)
        with self._lock:
            if self._closed:
                raise ProcessBoundaryUnavailable("Writer process domain is closed")
            _ensure_shared_runtime_read(path)
            self._allow_ancestor_metadata(path)
            self._runtime_roots.add(path)

    def _allow_ancestor_metadata(self, path):
        # GetFinalPathNameByHandle (Rust/Python canonicalize) examines ancestors.
        # These ACEs allow attributes/traversal only and do not inherit to files.
        for parent in path.parents:
            if parent == Path(parent.anchor) or parent in self._ancestor_grants:
                continue
            if any(parent.is_relative_to(root) for root, mask in self._grants.items()
                   if mask == 0x1F01FF and root.is_dir()):
                continue
            try:
                _modify_acl(parent, self._sid, 0x1200A0, inherit=False)
            except ProcessBoundaryUnavailable:
                # OS-owned ancestor directories cannot be changed by this user.
                # Do not broaden permissions or fall back to an unisolated run.
                continue
            self._ancestor_grants.add(parent)

    def allow_write(self, path):
        path = _safe_absolute(path, strict=False)
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True)
        self._grant(path, 0x1F01FF)

    def _grant(self, path, mask):
        path = _safe_absolute(path)
        with self._lock:
            if self._closed:
                raise ProcessBoundaryUnavailable("Writer process domain is closed")
            if self._grants.get(path, 0) & mask == mask:
                return
            # Existing wider parent grants cover this path without extra ACEs.
            if any(path.is_relative_to(parent) and previous & mask == mask
                   for parent, previous in self._grants.items() if parent.is_dir()):
                return
            _modify_acl(path, self._sid, mask)
            self._grants[path] = self._grants.get(path, 0) | mask
            if mask == 0x1F01FF:
                # Drive-root inherited "Modify" omits WRITE_OWNER, which is
                # needed to set a low integrity label. Preserve the local
                # owner's full control on these private Writer paths.
                _modify_acl(path, self._host_sid, 0x1F01FF)
                _allow_low_integrity_write(path)
            self._allow_ancestor_metadata(path)

    def _prepare_executable(self, command, executable=None):
        candidate = executable or (command[0] if not isinstance(command, str) else None)
        if not candidate:
            raise ProcessBoundaryUnavailable("Writer launch requires an explicit executable")
        import shutil
        resolved = Path(shutil.which(str(candidate)) or str(candidate)).resolve(strict=True)
        # Do not turn an executable path, .pth file or Writer-editable
        # pyvenv.cfg into a new read grant. Trusted runtimes are authorized by
        # the host explicitly; a changed home= entry gains no extra access.
        return resolved

    def popen(self, command, **kwargs):
        if self._closed:
            raise ProcessBoundaryUnavailable("Writer process domain is closed")
        if kwargs.get("shell"):
            raise ValueError("Use an explicit shell executable inside the Writer boundary")
        executable = self._prepare_executable(command, kwargs.get("executable"))
        allow_network = kwargs.pop("allow_network", True)
        kwargs["executable"] = str(executable)
        # Windows treats environment names case-insensitively. Duplicate Path /
        # PATH entries can break child PowerShell and Start-Process calls.
        env = {str(key).upper(): str(value) for key, value in
               (os.environ if kwargs.get("env") is None else kwargs["env"]).items()}
        # Never pass the interactive user's cache/config roots to a Writer.
        env.update({"HOME": str(self.private_home), "USERPROFILE": str(self.private_home),
                    "APPDATA": str(self.private_home / "AppData" / "Roaming"),
                    "LOCALAPPDATA": str(self.private_home / "AppData" / "Local"),
                    "TEMP": str(self.private_temp), "TMP": str(self.private_temp)})
        for name in ("APPDATA", "LOCALAPPDATA"):
            Path(env[name]).mkdir(parents=True, exist_ok=True)
        kwargs["env"] = env
        kwargs.setdefault("cwd", str(self.workspace))
        kwargs.setdefault("creationflags", subprocess.CREATE_NO_WINDOW)
        process = _BoundaryPopen(command, _domain=self, _allow_network=allow_network, **kwargs)
        self._processes.append(process)
        return process

    def run(self, command, *, input=None, capture_output=False, timeout=None,
            check=False, **kwargs):
        if input is not None:
            if kwargs.get("stdin") is not None:
                raise ValueError("stdin and input cannot both be supplied")
            kwargs["stdin"] = subprocess.PIPE
        if capture_output:
            if kwargs.get("stdout") is not None or kwargs.get("stderr") is not None:
                raise ValueError("capture_output conflicts with stdout/stderr")
            kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        with self.popen(command, **kwargs) as process:
            try:
                stdout, stderr = process.communicate(input, timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill_tree()
                stdout, stderr = process.communicate()
                raise subprocess.TimeoutExpired(command, timeout, output=stdout, stderr=stderr)
            result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
            if check:
                result.check_returncode()
            return result

    def _validate_child(self, handle):
        token = wt.HANDLE()
        _check(_advapi.OpenProcessToken(handle, 8, ctypes.byref(token)), "Inspect AppContainer child")
        try:
            app = _token_data(token, 29)  # TokenIsAppContainer
            if not ctypes.cast(app, ctypes.POINTER(wt.DWORD)).contents.value:
                raise ProcessBoundaryUnavailable("Child is not an AppContainer; execution refused")
            info = _token_data(token, 31)  # TokenAppContainerSid
            sid = ctypes.cast(info, ctypes.POINTER(wt.LPVOID)).contents.value
            if not _advapi.EqualSid(sid, self._sid):
                raise ProcessBoundaryUnavailable("Child has the wrong Writer identity; execution refused")
            integrity = _token_data(token, 25)  # TokenIntegrityLevel
            level = _sid_string(ctypes.cast(integrity, ctypes.POINTER(_SidAttributes)).contents.Sid)
            if int(level.rsplit("-", 1)[1]) > 4096:
                raise ProcessBoundaryUnavailable("Writer child integrity was not lowered")
        finally:
            _kernel.CloseHandle(token)

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for process in self._processes:
                process._close_job()
            for path in reversed(self._grants):
                try:
                    if path.exists():
                        _modify_acl(path, self._sid, 0, revoke=True)
                except Exception:
                    _LOG.exception("Could not remove Writer-specific ACL from %s", path)
            for path in self._ancestor_grants - self._grants.keys():
                try:
                    if path.exists():
                        _modify_acl(path, self._sid, 0, revoke=True, inherit=False)
                except Exception:
                    _LOG.exception("Could not remove Writer ancestor metadata ACL from %s", path)
            if self._sid:
                result = _userenv.DeleteAppContainerProfile(self.profile)
                if result:
                    _LOG.warning("Writer profile cleanup returned %#x: %s", result, self.profile)
                _advapi.FreeSid(self._sid)
                self._sid = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class _BoundaryPopen(subprocess.Popen):
    def __init__(self, *args, _domain, _allow_network=True, **kwargs):
        self._domain = _domain
        self._allow_network = _allow_network
        self._job = None
        super().__init__(*args, **kwargs)

    def _execute_child(self, args, executable, preexec_fn, close_fds, pass_fds,
                       cwd, env, startupinfo, creationflags, shell,
                       p2cread, p2cwrite, c2pread, c2pwrite, errread, errwrite, *unused):
        if shell or pass_fds or preexec_fn:
            raise ValueError("Unsupported process option for Windows Writer boundary")
        if startupinfo and getattr(startupinfo, "lpAttributeList", None):
            raise ValueError("Custom inherited handles are not allowed in a Writer boundary")
        command = args if isinstance(args, str) else subprocess.list2cmdline(args)
        info = _StartupInfoEx()
        info.StartupInfo.cb = ctypes.sizeof(info)
        info.StartupInfo.dwFlags = 1  # STARTF_USESHOWWINDOW
        info.StartupInfo.wShowWindow = 0
        handles = []
        if -1 not in (p2cread, c2pwrite, errwrite):
            info.StartupInfo.dwFlags |= 0x100
            info.StartupInfo.hStdInput = int(p2cread)
            info.StartupInfo.hStdOutput = int(c2pwrite)
            info.StartupInfo.hStdError = int(errwrite)
            handles = self._filter_handle_list([int(p2cread), int(c2pwrite), int(errwrite)])
        internet = wt.LPVOID()
        _check(_advapi.ConvertStringSidToSidW("S-1-15-3-1", ctypes.byref(internet)), "Internet capability")
        capabilities_list = [_SidAttributes(_readonly_capability(), 4)]
        if self._allow_network:
            capabilities_list.append(_SidAttributes(internet, 4))
        capability_array = (_SidAttributes * len(capabilities_list))(*capabilities_list)
        capabilities = _Capabilities(self._domain._sid, capability_array, len(capabilities_list), 0)
        size = ctypes.c_size_t()
        _kernel.InitializeProcThreadAttributeList(None, 1 + bool(handles), 0, ctypes.byref(size))
        attributes = ctypes.create_string_buffer(size.value)
        _check(_kernel.InitializeProcThreadAttributeList(attributes, 1 + bool(handles),
                                                        0, ctypes.byref(size)), "Create process attributes")
        info.lpAttributeList = ctypes.cast(attributes, wt.LPVOID)
        process = _ProcessInfo()
        job = None
        try:
            _check(_kernel.UpdateProcThreadAttribute(attributes, 0, 0x20009,
                   ctypes.byref(capabilities), ctypes.sizeof(capabilities), None, None), "Apply AppContainer identity")
            if handles:
                inherited = (wt.HANDLE * len(handles))(*handles)
                _check(_kernel.UpdateProcThreadAttribute(attributes, 0, 0x20002,
                       inherited, ctypes.sizeof(inherited), None, None), "Restrict inherited handles")
            # Job handles and host process/token handles are never inherited.
            job = _kernel.CreateJobObjectW(None, None)
            _check(job, "Create isolated run job")
            limit = _JobLimit()
            limit.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE, no breakaway
            _check(_kernel.SetInformationJobObject(job, 9, ctypes.byref(limit),
                                                   ctypes.sizeof(limit)), "Configure isolated run job")
            block = ctypes.create_unicode_buffer("\0".join(
                f"{key}={value}" for key, value in sorted(env.items(), key=lambda item: item[0].upper())) + "\0\0")
            flags = creationflags | 0x80000 | 0x400 | 4  # EXTENDED, UNICODE_ENV, SUSPENDED
            _check(_kernel.CreateProcessW(executable, ctypes.create_unicode_buffer(command),
                   None, None, bool(handles), flags, block, str(cwd), ctypes.byref(info),
                   ctypes.byref(process)), "Create isolated Writer process")
            self._domain._validate_child(process.hProcess)
            _check(_kernel.AssignProcessToJobObject(job, process.hProcess), "Attach isolated run job")
            _check(_kernel.ResumeThread(process.hThread) != 0xFFFFFFFF, "Resume isolated Writer")
            self._job = job
            job = None
            self._child_created = True
            self._handle = subprocess.Handle(process.hProcess)
            self.pid = process.dwProcessId
            process.hProcess = None
        except BaseException:
            if process.hProcess:
                _kernel.TerminateProcess(process.hProcess, 1)
            raise
        finally:
            if process.hThread:
                _kernel.CloseHandle(process.hThread)
            if process.hProcess:
                _kernel.CloseHandle(process.hProcess)
            if job:
                _kernel.CloseHandle(job)
            _kernel.DeleteProcThreadAttributeList(attributes)
            _kernel.LocalFree(internet)
            self._close_pipe_fds(p2cread, p2cwrite, c2pread, c2pwrite, errread, errwrite)

    def kill_tree(self):
        if self._job:
            _check(_kernel.TerminateJobObject(self._job, 1), "Stop this Writer's run")

    def _close_job(self):
        if self._job:
            _kernel.CloseHandle(self._job)
            self._job = None

    def close_run(self):
        """Release this run's job, ending only its remaining descendants."""
        self._close_job()

    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self._close_job()
