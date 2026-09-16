"""Admission: is this item quiet enough to look at yet?

Quiescence only. Admission grants no ownership and proves nothing about who has
the file open — that is the eviction-time check's job. An item that is
not admitted is simply looked at again on a later run.
"""

from __future__ import annotations

import os
from pathlib import Path
import time

from .walk import SpecialEntry, walk


def now() -> float:
    return time.time()


def sleep(seconds: float) -> None:
    time.sleep(seconds)


def admit(path: Path | str, *, settle_seconds: float,
          sample_gap_seconds: float) -> bool:
    path = Path(path)
    try:
        first = _sample(path)
    except (OSError, SpecialEntry):
        return False
    if not first:
        return False

    newest = max(entry[3] for entry in first)
    if now() - (newest / 1e9) < settle_seconds:
        return False

    if sample_gap_seconds:
        sleep(sample_gap_seconds)
    try:
        second = _sample(path)
    except (OSError, SpecialEntry):
        return False
    return first == second


def _sample(path: Path):
    return [(entry.rel_path, entry.entry_type, entry.size_bytes, entry.mtime_ns,
             entry.ctime_ns, entry.inode, entry.dev, entry.link_target)
            for entry in walk(path)]
