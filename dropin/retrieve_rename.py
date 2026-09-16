"""Atomic no-replace restore publication; unsupported kernels fail closed.

Linux: linux/fs.h RENAME_NOREPLACE=1, libc renameat2(3).
Darwin: Apple xnu bsd/sys/stdio.h renameatx_np, RENAME_EXCL=0x4
(available macOS 10.12+). Never fall back to check-then-rename.
"""
import ctypes
import errno
import os
import sys


def rename_noreplace(source_fd, source, destination_fd, destination):
    if sys.platform == "linux":
        symbol, flag = "renameat2", 1
    elif sys.platform == "darwin":
        symbol, flag = "renameatx_np", 4
    else:
        raise OSError(errno.ENOTSUP, "atomic no-replace rename is unsupported")
    library = ctypes.CDLL(None, use_errno=True)
    try:
        function = getattr(library, symbol)
    except AttributeError as error:
        raise OSError(errno.ENOTSUP, f"{symbol} is unavailable; refusing publication") from error
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                         ctypes.c_char_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    if function(source_fd, os.fsencode(source), destination_fd,
                os.fsencode(destination), flag) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), os.fspath(destination))
