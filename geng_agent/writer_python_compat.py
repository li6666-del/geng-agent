"""CPython mkdir(0700) compatibility inside a Windows AppContainer only.

CPython's protected owner-only ACL otherwise omits the AppContainer SID
(python/cpython#134587). This adds just the current container to that ACL.
The module is copied into private Writer environments, never a shared Python.
It is a compatibility shim, not the security boundary; Windows enforces that.
"""
from __future__ import annotations

import os


def install():
    if os.name != "nt" or getattr(os.mkdir, "_geng_appcontainer_compat", False):
        return
    import ctypes as c
    from ctypes import wintypes as w

    kernel = c.WinDLL("kernel32", use_last_error=True)
    advapi = c.WinDLL("advapi32", use_last_error=True)

    def bind(dll, name, args, result=w.BOOL):
        function = getattr(dll, name)
        function.argtypes, function.restype = args, result
        return function

    bind(kernel, "GetCurrentProcess", [], w.HANDLE)
    bind(kernel, "CloseHandle", [w.HANDLE])
    bind(kernel, "LocalFree", [w.LPVOID], w.LPVOID)
    bind(advapi, "OpenProcessToken", [w.HANDLE, w.DWORD, c.POINTER(w.HANDLE)])
    bind(advapi, "GetTokenInformation", [w.HANDLE, c.c_int, w.LPVOID, w.DWORD, c.POINTER(w.DWORD)])
    bind(advapi, "ConvertSidToStringSidW", [w.LPVOID, c.POINTER(w.LPWSTR)])
    bind(advapi, "ConvertStringSecurityDescriptorToSecurityDescriptorW",
         [w.LPCWSTR, w.DWORD, c.POINTER(w.LPVOID), w.LPVOID])
    token, text, descriptor = w.HANDLE(), w.LPWSTR(), w.LPVOID()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 8, c.byref(token)):
        raise c.WinError(c.get_last_error())
    try:
        app, size = w.DWORD(), w.DWORD()
        if not advapi.GetTokenInformation(token, 29, c.byref(app), 4, c.byref(size)):
            raise c.WinError(c.get_last_error())
        if not app.value:
            return
        advapi.GetTokenInformation(token, 31, None, 0, c.byref(size))
        info = c.create_string_buffer(size.value)
        if not advapi.GetTokenInformation(token, 31, info, size, c.byref(size)):
            raise c.WinError(c.get_last_error())
        sid = c.cast(info, c.POINTER(w.LPVOID)).contents.value
        if not advapi.ConvertSidToStringSidW(sid, c.byref(text)):
            raise c.WinError(c.get_last_error())
        sddl = "D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;OW)(A;OICI;FA;;;" + text.value + ")"
        if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, c.byref(descriptor), None):
            raise c.WinError(c.get_last_error())
    finally:
        if text:
            kernel.LocalFree(text)
        kernel.CloseHandle(token)

    class SecurityAttributes(c.Structure):
        _fields_ = [("length", w.DWORD), ("descriptor", w.LPVOID), ("inherit", w.BOOL)]

    bind(kernel, "CreateDirectoryW", [w.LPCWSTR, c.POINTER(SecurityAttributes)])
    original = os.mkdir

    def mkdir(path, mode=0o777, *, dir_fd=None):
        if mode != 0o700 or dir_fd is not None:
            return original(path, mode, dir_fd=dir_fd)
        name = os.fsdecode(os.fspath(path))
        if "\x00" in name:
            raise ValueError("embedded null character")
        attributes = SecurityAttributes(c.sizeof(SecurityAttributes), descriptor, False)
        if not kernel.CreateDirectoryW(name, c.byref(attributes)):
            error = c.WinError(c.get_last_error())
            error.filename = path
            raise error

    mkdir._geng_appcontainer_compat = True
    # The small descriptor belongs to this process and stays alive with mkdir.
    os.mkdir = mkdir


install()
