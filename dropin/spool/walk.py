"""Preflight walk: the expected manifest's raw material.

Never follows symbolic links, and refuses the whole tree on the first special
entry because restic would happily archive a FIFO as a node and the
refusal has to happen before any transfer.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import stat
from typing import Iterator

SPECIAL_TYPES = {
    stat.S_IFIFO: "fifo",
    stat.S_IFSOCK: "socket",
    stat.S_IFCHR: "device",
    stat.S_IFBLK: "device",
}


class SpecialEntry(Exception):
    """An unsupported filesystem entry refuses the entire tree."""

    def __init__(self, rel_path: str, entry_type: str) -> None:
        super().__init__(f"{entry_type} at {rel_path or '.'}")
        self.rel_path = rel_path
        self.entry_type = entry_type


@dataclass(frozen=True)
class WalkEntry:
    rel_path: str
    entry_type: str
    size_bytes: int | None
    mode: int
    mtime_ns: int
    ctime_ns: int
    inode: int
    dev: int
    link_target: str | None


def walk(root: Path | str) -> Iterator[WalkEntry]:
    """Yield the root entry, then every descendant, ordered by relative path."""
    root = Path(root)
    yield _entry(root, "")
    root_stat = os.lstat(root)
    if not stat.S_ISDIR(root_stat.st_mode):
        return
    yield from _descend(root, "")


def _descend(root: Path, rel: str) -> Iterator[WalkEntry]:
    directory = root / rel if rel else root
    names = sorted(os.listdir(directory), key=os.fsencode)
    for name in names:
        child_rel = f"{rel}/{name}" if rel else name
        entry = _entry(root / child_rel, child_rel)
        yield entry
        if entry.entry_type == "dir":
            yield from _descend(root, child_rel)


def _entry(path: Path, rel: str) -> WalkEntry:
    info = os.lstat(path)
    mode = info.st_mode
    file_type = stat.S_IFMT(mode)
    if file_type in SPECIAL_TYPES:
        raise SpecialEntry(rel, SPECIAL_TYPES[file_type])
    if stat.S_ISLNK(mode):
        entry_type, link_target, size = "symlink", os.readlink(path), None
    elif stat.S_ISDIR(mode):
        entry_type, link_target, size = "dir", None, None
    elif stat.S_ISREG(mode):
        entry_type, link_target, size = "file", None, info.st_size
    else:
        raise SpecialEntry(rel, "unsupported")
    return WalkEntry(rel_path=rel, entry_type=entry_type, size_bytes=size,
                     mode=mode, mtime_ns=info.st_mtime_ns,
                     ctime_ns=info.st_ctime_ns, inode=info.st_ino,
                     dev=info.st_dev, link_target=link_target)
