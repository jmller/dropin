"""Linux creation-to-open provenance guard for OwnedDirectory.

mkdirat returns no fd. A first stat/open alone can adopt a replacement. Watch
namespace changes BEFORE mkdir, pin the candidate, then fence parent mutations
before consuming the event queue. The guard is deliberately limited and
unsupported platforms/filesystems fail closed.
Unsupported platforms/filesystems fail closed; no stat-only fallback.
"""
import ctypes
import errno
import os
import platform
import struct
import sys


# Linux UAPI inotify.h. Events for children include IN_ISDIR where applicable.
IN_ATTRIB = 0x00000004
IN_MOVED_FROM = 0x00000040
IN_MOVED_TO = 0x00000080
IN_CREATE = 0x00000100
IN_DELETE = 0x00000200
IN_DELETE_SELF = 0x00000400
IN_MOVE_SELF = 0x00000800
IN_UNMOUNT = 0x00002000
IN_Q_OVERFLOW = 0x00004000
IN_IGNORED = 0x00008000
IN_ONLYDIR = 0x01000000
IN_ISDIR = 0x40000000
WATCH_MASK = (IN_ATTRIB | IN_MOVED_FROM | IN_MOVED_TO | IN_CREATE | IN_DELETE
              | IN_DELETE_SELF | IN_MOVE_SELF | IN_ONLYDIR)
EVENT = struct.Struct('=iIII')
# Local Linux VFS filesystems only. Network/FUSE/unknown filesystems can change
# out of band without a local inotify event and therefore cannot grant authority.
LOCAL_FILESYSTEMS = {0xEF53, 0x58465342, 0x9123683E, 0x01021994, 0x794C7630}


class _StatFS(ctypes.Structure):
    # Linux x86_64/aarch64 LP64 libc statfs (glibc/musl), not every LP64 ABI.
    # MIPS64, for example, has a larger layout; reject before any native write.
    _fields_ = [('f_type', ctypes.c_long), ('f_bsize', ctypes.c_long),
                ('f_blocks', ctypes.c_ulong), ('f_bfree', ctypes.c_ulong),
                ('f_bavail', ctypes.c_ulong), ('f_files', ctypes.c_ulong),
                ('f_ffree', ctypes.c_ulong), ('f_fsid', ctypes.c_int * 2),
                ('f_namelen', ctypes.c_long), ('f_frsize', ctypes.c_long),
                ('f_flags', ctypes.c_long), ('f_spare', ctypes.c_long * 4)]


def _native(library, name, arguments):
    try:
        function = getattr(library, name)
    except AttributeError as error:
        raise OSError(errno.ENOTSUP, f'acquisition requires {name}') from error
    function.argtypes = arguments
    function.restype = ctypes.c_int
    return function


def _checked(result):
    if result < 0:
        code = ctypes.get_errno()
        raise OSError(code, f'acquisition guard: {os.strerror(code)}')
    return result


def _filesystem_type(fd):
    library = ctypes.CDLL(None, use_errno=True)
    function = _native(library, 'fstatfs', [ctypes.c_int, ctypes.POINTER(_StatFS)])
    result = _StatFS()
    _checked(function(fd, ctypes.byref(result)))
    return result.f_type


class AcquisitionGuard:
    def __init__(self, parent_fd, name):
        self.parent_fd = parent_fd
        self.name = name
        self.fd = None

    def __enter__(self):
        # Darwin has no inotify equivalent used here. Continue with the
        # descriptor/identity checks below; the random private stage and atomic
        # no-replace publication are the practical local-user boundary.
        if sys.platform == 'darwin':
            return self
        if (sys.platform != 'linux' or platform.machine() not in ('x86_64', 'aarch64')
                or ctypes.sizeof(ctypes.c_long) != 8 or ctypes.sizeof(ctypes.c_void_p) != 8):
            raise OSError(errno.ENOTSUP, 'directory acquisition requires the Linux x86_64/aarch64 LP64 guard; no unsafe fallback')
        if _filesystem_type(self.parent_fd) not in LOCAL_FILESYSTEMS:
            raise OSError(errno.ENOTSUP, 'directory acquisition requires a supported local filesystem')
        library = ctypes.CDLL(None, use_errno=True)
        init = _native(library, 'inotify_init1', [ctypes.c_int])
        add = _native(library, 'inotify_add_watch', [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32])
        self.fd = _checked(init(os.O_NONBLOCK | os.O_CLOEXEC))
        try:
            # Intentional proc-fd resolution: it names our already pinned parent,
            # not the caller's mutable destination spelling. No proc fallback.
            self.wd = _checked(add(self.fd, os.fsencode(f'/proc/self/fd/{self.parent_fd}'), WATCH_MASK))
        except BaseException:
            os.close(self.fd)
            self.fd = None
            raise
        return self

    def __exit__(self, *args):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def verify(self):
        # A directory lookup/open may observe a rename before fsnotify_move is
        # queued. filename_create's EEXIST path takes the parent's inode lock;
        # vfs_{mkdir,rename,rmdir} queue notifications before releasing that lock.
        # Thus changes responsible for the previously opened fd are now queued.
        # If the name vanished and this mkdir succeeds, retain it and refuse.
        try:
            os.mkdir(self.name, 0o700, dir_fd=self.parent_fd)
        except FileExistsError:
            pass
        else:
            raise OSError('acquisition barrier unexpectedly created a directory; retained, path uncertain')
        if sys.platform == 'darwin':
            return
        created = 0
        name = os.fsencode(self.name)
        # Bounded work even under an unrelated-event flood. Lost events or an
        # unprovable queue state always refuse; they are not an invitation to retry.
        for _ in range(16):
            try:
                data = os.read(self.fd, 65536)
            except BlockingIOError:
                if created == 1:
                    return
                raise OSError('acquisition creation event missing')
            if not data:
                raise OSError('acquisition event stream unexpectedly ended')
            offset = 0
            while offset < len(data):
                if len(data) - offset < EVENT.size:
                    raise OSError('acquisition event header truncated')
                wd, mask, cookie, length = EVENT.unpack_from(data, offset)
                offset += EVENT.size
                end = offset + length
                if end > len(data):
                    raise OSError('acquisition event name truncated')
                raw = data[offset:end]
                if raw and b'\0' not in raw:
                    raise OSError('acquisition event name not terminated')
                event_name = raw.split(b'\0', 1)[0]
                offset = end
                if mask & (IN_Q_OVERFLOW | IN_IGNORED | IN_UNMOUNT) or wd != self.wd:
                    raise OSError('acquisition watch lost events or became unavailable')
                if not event_name:
                    raise OSError('acquisition parent identity or attributes changed')
                if event_name == name:
                    if mask != IN_CREATE | IN_ISDIR or cookie or created:
                        raise OSError('acquisition namespace changed; refusing substituted directory')
                    created += 1
        raise OSError('acquisition event budget exceeded; refusing uncertain directory')
