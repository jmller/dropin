"""One writer per state directory.

`flock` is the authority; the JSON body is advisory detail so a refusal can name
the holder. A crashed holder releases the lock with its file descriptor, so no
stale-lock cleanup is needed — and none is offered, because "clean up the stale
lock" is how two writers end up running.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path


class LockHeld(Exception):
    """Another invocation holds the state directory."""

    def __init__(self, pid: int | None, verb: str | None, since: str | None) -> None:
        if pid is None:
            message = "another dropin invocation holds the state directory"
        else:
            message = f"another dropin {verb} is running (pid {pid} since {since})"
        super().__init__(message)
        self.pid = pid
        self.verb = verb
        self.since = since


@contextmanager
def writer_lock(path: Path | str, *, verb: str):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise _held(path) from None
        record = {"pid": os.getpid(), "verb": verb,
                  "since": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        os.ftruncate(handle, 0)
        os.write(handle, json.dumps(record).encode())
        os.fsync(handle)
        yield record
    finally:
        os.close(handle)


def current_holder(path: Path | str) -> dict | None:
    """Return the authoritative current holder without creating or changing the lock.

    The JSON body is read only after a shared nonblocking flock proves that an
    exclusive writer currently holds the inode. Stale advisory text is ignored.
    """
    path = Path(path)
    try:
        handle = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        return None
    except OSError as error:
        return {"unknown": True, "reason": str(error)}
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError:
            try:
                os.lseek(handle, 0, os.SEEK_SET)
                record = json.loads(os.read(handle, 8192).decode())
                return {"pid": int(record["pid"]), "verb": record.get("verb"),
                        "since": record.get("since")}
            except (OSError, UnicodeError, ValueError, KeyError, TypeError) as error:
                return {"unknown": True, "reason": f"holder details unavailable: {error}"}
        return None
    finally:
        os.close(handle)


def _held(path: Path) -> LockHeld:
    try:
        record = json.loads(path.read_text())
        return LockHeld(int(record["pid"]), record.get("verb"), record.get("since"))
    except (OSError, ValueError, KeyError, TypeError):
        # The body is advisory: an unreadable one must not turn a refusal into
        # a crash, or into permission to proceed.
        return LockHeld(None, None, None)
