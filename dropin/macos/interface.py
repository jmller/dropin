"""The macOS seam.

Everything Spotlight-shaped goes through this protocol so the whole pipeline is
testable on Linux. The ownership check lives here too, because on macOS it is
the same kind of shell-out — and because it must fail closed: an adapter that
cannot prove the check works reports `unsupported`, and eviction stops.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..capture.mdls_parser import Attr


class CaptureFailure(Exception):
    """Metadata could not be captured for this entry. Recorded, never fatal."""


class Unsupported(Exception):
    """The runtime cannot perform this check. Eviction must stop, not guess."""


@dataclass(frozen=True)
class XattrValue:
    value: bytes | None
    status: str  # 'ok' | 'skipped:<reason>'


@dataclass(frozen=True)
class Capabilities:
    ownership_check: bool
    ownership_reason: str = ""
    scope_note: str = ""


@dataclass(frozen=True)
class LaunchAgent:
    path: Path
    label: str
    program: tuple[str, ...]
    queue_directories: tuple[str, ...]
    interval: int


@runtime_checkable
class MacOS(Protocol):
    validation_state: str

    def mdls(self, path: str) -> dict[str, Attr]: ...

    def importer_attributes(self, path: str) -> dict[str, Attr]: ...

    def xattrs(self, path: str) -> dict[str, XattrValue]: ...

    def finder_tags(self, path: str) -> list[str]: ...

    def finder_comment(self, path: str) -> str | None: ...

    def is_bundle(self, path: str) -> bool: ...

    def open_descriptors(self, path: str, is_dir: bool) -> list[int]: ...

    def capabilities(self) -> Capabilities: ...

    def write_launch_agent(self, path: Path, *, label: str, program,
                           queue_directories, interval: int) -> None: ...


#: A directory is a bundle only if its content-type tree says so. `kMDItemKind`
#: is a display string ("Folder", "Application") and must never decide.
BUNDLE_TYPES = ("com.apple.package", "com.apple.bundle")
TAGS_XATTR = "com.apple.metadata:_kMDItemUserTags"
COMMENT_XATTR = "com.apple.metadata:kMDItemFinderComment"


def is_bundle_tree(tree) -> bool:
    return any(entry in BUNDLE_TYPES for entry in (tree or ()))
