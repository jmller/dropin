"""Top-level spool scan.

Only top-level entries are items: a directory is one item, not a source of
items. Ordering is by name bytes so a run is reproducible regardless of locale.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

#: Finder droppings and our own restore scratch are never items.
IGNORED_EXACT = {".DS_Store", ".localized"}
IGNORED_PREFIXES = (".dropin-",)


def scan(drop_dir: Path | str) -> Iterator[Path]:
    drop_dir = Path(drop_dir)
    try:
        names = os.listdir(drop_dir)
    except FileNotFoundError:
        return iter(())
    selected = [name for name in names if not _ignored(name)]
    selected.sort(key=lambda name: os.fsencode(name))
    return iter([drop_dir / name for name in selected])


def _ignored(name: str) -> bool:
    return name in IGNORED_EXACT or name.startswith(IGNORED_PREFIXES)
