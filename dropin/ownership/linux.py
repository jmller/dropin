"""Linux open-descriptor adapter.

Development runs here, so this is the adapter the Linux integration tests use.
It scans `/proc/<pid>/fd` for symlinks resolving to the target, after proving
through `/proc/self/fd` that it can see one of its own — the same
positive/negative control the macOS adapter applies, for the same reason: an
empty result from a check that cannot see anything would silently authorise a
deletion.

Stated scope, not hidden: only processes visible to the invoking user are
scanned, and memory mappings without an open descriptor are not covered.
"""

from __future__ import annotations

import os
from pathlib import Path
import tempfile

from ..macos.interface import Capabilities, Unsupported

SCOPE_NOTE = ("visible to the invoking user only; other users' /proc entries "
              "are not readable, and mmap-only access without an open "
              "descriptor is not covered")


class LinuxOwnership:
    def __init__(self, proc_root: str = "/proc") -> None:
        self.proc_root = proc_root

    # ---- capability --------------------------------------------------------

    def capabilities(self) -> Capabilities:
        try:
            self._require_proc()
            with tempfile.TemporaryDirectory(prefix="dropin-own-probe-") as temp:
                probe = Path(temp) / "probe"
                probe.write_bytes(b"probe")
                with probe.open("rb"):
                    if os.getpid() not in self._holders(str(probe), is_dir=False):
                        return Capabilities(
                            False,
                            "positive control failed: our own open descriptor "
                            "was not visible, so an empty result cannot mean "
                            "'nobody has it open'",
                            SCOPE_NOTE)
                if os.getpid() in self._holders(str(probe), is_dir=False):
                    return Capabilities(
                        False,
                        "negative control failed: a closed file still reports a "
                        "holder",
                        SCOPE_NOTE)
        except (Unsupported, OSError) as error:
            return Capabilities(False, str(error), SCOPE_NOTE)
        return Capabilities(True, "", SCOPE_NOTE)

    def _require_proc(self) -> None:
        self_fd = os.path.join(self.proc_root, "self", "fd")
        if not os.path.isdir(self_fd):
            raise Unsupported(f"{self_fd} is not available")
        try:
            os.listdir(self_fd)
        except OSError as error:
            raise Unsupported(f"cannot read {self_fd}: {error}") from error

    # ---- check -------------------------------------------------------------

    def open_descriptors(self, path: str, is_dir: bool) -> list[int]:
        self._require_proc()
        return self._holders(path, is_dir)

    def _holders(self, path: str, is_dir: bool) -> list[int]:
        target = os.path.realpath(path)
        prefix = target.rstrip("/") + "/"
        pids: list[int] = []
        for entry in os.listdir(self.proc_root):
            if not entry.isdigit():
                continue
            fd_dir = os.path.join(self.proc_root, entry, "fd")
            try:
                names = os.listdir(fd_dir)
            except OSError:
                # Another user's process, or one that exited mid-scan. Not an
                # error: it is exactly the scope limit stated above.
                continue
            for name in names:
                try:
                    resolved = os.readlink(os.path.join(fd_dir, name))
                except OSError:
                    continue
                if resolved == target or (is_dir and resolved.startswith(prefix)):
                    pids.append(int(entry))
                    break
        return sorted(set(pids))
