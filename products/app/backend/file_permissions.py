"""Private application files on POSIX and Windows, without shell commands."""
from __future__ import annotations

import os
from pathlib import Path


def protect_private_path(path: Path) -> None:
    if path.is_symlink():
        raise OSError(f"Private storage cannot be a symbolic link: {path}")
    if os.name != "nt":
        os.chmod(path, 0o700 if path.is_dir() else 0o600)
        return
    _protect_windows_path(path)


def _protect_windows_path(path: Path) -> None:
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    pointer = ctypes.c_void_p
    signatures = (
        (kernel.GetCurrentProcess, [], wintypes.HANDLE),
        (kernel.CloseHandle, [wintypes.HANDLE], wintypes.BOOL),
        (kernel.LocalFree, [pointer], pointer),
        (security.OpenProcessToken, [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)], wintypes.BOOL),
        (security.GetTokenInformation, [wintypes.HANDLE, ctypes.c_int, pointer, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
        (security.ConvertSidToStringSidW, [pointer, ctypes.POINTER(pointer)], wintypes.BOOL),
        (security.ConvertStringSecurityDescriptorToSecurityDescriptorW, [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(pointer), pointer], wintypes.BOOL),
        (security.SetFileSecurityW, [wintypes.LPCWSTR, wintypes.DWORD, pointer], wintypes.BOOL),
    )
    for function, arguments, result in signatures:
        function.argtypes = arguments
        function.restype = result

    def checked(success):
        if not success:
            raise ctypes.WinError(ctypes.get_last_error())

    token = wintypes.HANDLE()
    sid_text = pointer()
    descriptor = pointer()
    checked(security.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)))
    try:
        size = wintypes.DWORD()
        security.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        if not size.value:
            raise ctypes.WinError(ctypes.get_last_error())
        user = ctypes.create_string_buffer(size.value)
        checked(security.GetTokenInformation(token, 1, user, size, ctypes.byref(size)))
        # TOKEN_USER begins with SID_AND_ATTRIBUTES.Sid on both x86 and x64.
        sid = ctypes.cast(user, ctypes.POINTER(pointer))[0]
        checked(security.ConvertSidToStringSidW(sid, ctypes.byref(sid_text)))
        inheritance = "OICI" if path.is_dir() else ""
        sddl = f"D:P(A;{inheritance};FA;;;{ctypes.wstring_at(sid_text)})"
        checked(security.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None))
        # Replace the DACL and disable inherited grants; do not change ownership.
        checked(security.SetFileSecurityW(str(path), 0x80000004, descriptor))
    finally:
        if descriptor:
            kernel.LocalFree(descriptor)
        if sid_text:
            kernel.LocalFree(sid_text)
        kernel.CloseHandle(token)
