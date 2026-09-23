"""Windows source protection for a copy-then-delete move.

Keep a read/delete handle with only FILE_SHARE_READ for the entire move.
Delete through that handle, never by reopening a potentially replaced path.
"""
from __future__ import annotations

from contextlib import contextmanager
import os


class LockedMoveSource:
    def __init__(self, stream, delete):
        self.stream = stream
        self.delete = delete


@contextmanager
def locked_move_source(path: str):
    if os.name != "nt":
        # Advisory POSIX locks cannot exclude unrelated writers. Fail closed
        # instead of silently weakening the Windows application's guarantee.
        raise OSError("安全移动需要 Windows 文件共享锁；源文件未删除")

    import ctypes
    from ctypes import wintypes
    import msvcrt

    api = ctypes.WinDLL("kernel32", use_last_error=True)
    create = api.CreateFileW
    create.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    create.restype = wintypes.HANDLE
    close = api.CloseHandle
    close.argtypes = (wintypes.HANDLE,)
    close.restype = wintypes.BOOL
    set_info = api.SetFileInformationByHandle
    set_info.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
    set_info.restype = wintypes.BOOL

    # Do not follow a file symlink and accidentally delete its referent.
    if os.path.islink(path):
        raise OSError("暂不支持移动文件符号链接；源文件未删除")
    handle = create(os.path.abspath(path), 0x80000000 | 0x00010000,
                    0x1, None, 3, 0x00200000, None)
    # GENERIC_READ | DELETE, FILE_SHARE_READ, OPEN_EXISTING,
    # FILE_FLAG_OPEN_REPARSE_POINT (also closes a symlink substitution race).
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        close(handle)
        raise
    try:
        stream = os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise

    def delete_locked_file():
        # FILE_DISPOSITION_INFO is a single BOOL. Deletion happens on close.
        disposition = wintypes.BOOL(True)
        if not set_info(handle, 4, ctypes.byref(disposition), ctypes.sizeof(disposition)):
            raise ctypes.WinError(ctypes.get_last_error())

    with stream:
        yield LockedMoveSource(stream, delete_locked_file)
