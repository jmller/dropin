"""Captured values, before anything is written to the store."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..macos.interface import XattrValue
from ..spool.walk import WalkEntry
from .mdls_parser import Attr


@dataclass
class CapturedEntry:
    rel_path: str
    entry_type: str
    size_bytes: int | None
    sha256: str | None
    link_target: str | None
    mode: int
    fingerprint: WalkEntry
    searchable: bool = True
    capture_status: str = "ok"
    #: keyed by (attribute key, source) so an importer value never overwrites an
    #: mdls value of the same name — both are evidence.
    attributes: dict[tuple[str, str], Attr] = field(default_factory=dict)
    xattrs: dict[str, XattrValue] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    comment: str | None = None
    text: str | None = None

    @property
    def uti(self) -> str | None:
        return self._attribute("kMDItemContentType")

    @property
    def kind(self) -> str | None:
        return self._attribute("kMDItemKind")

    @property
    def created(self) -> str | None:
        return self._attribute("kMDItemContentCreationDate")

    @property
    def modified(self) -> str | None:
        return self._attribute("kMDItemContentModificationDate")

    def _attribute(self, key: str) -> str | None:
        for source in ("mdls", "importer"):
            found = self.attributes.get((key, source))
            if found is not None and found.value is not None:
                value = found.value
                # Only kind is a localized display string. Keep the complete
                # map in attributes; never stringify collections into search.
                if key == "kMDItemKind" and isinstance(value, dict):
                    value = value.get("")
                return value if isinstance(value, str) else None
        return None


@dataclass
class CapturedItem:
    spool_path: str
    item_name: str
    kind: str
    root_sha256: str
    size_bytes: int
    entries: list[CapturedEntry]

    @property
    def entry_count(self) -> int:
        return len(self.entries)
