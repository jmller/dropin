"""Capture: metadata and hashes, before any transfer.

Ordering is the point. The source fingerprint is sampled
*before* hashing and again *after*; only if both samples agree is anything
recorded. Otherwise the recorded hash could describe bytes that no longer exist,
and every later gate would compare against a fiction.

Metadata failures are recorded in `capture_status` and never fail the archive:
losing a Finder comment is not worth refusing to archive the file. A special
filesystem entry is different — that refuses the whole tree, before any metadata
work, because it cannot be archived faithfully at all.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ..macos.interface import CaptureFailure
from ..spool.hashing import sha256_stream
from ..spool.walk import WalkEntry, walk
from .model import CapturedEntry, CapturedItem

#: The canonical manifest serialisation. Any change here changes every stored
#: `root_sha256`, so it is a schema change, not a refactor.
MANIFEST_FIELDS = ("rel_path", "entry_type", "size_bytes", "sha256", "link_target")


class CaptureAborted(Exception):
    """The source changed while it was being captured; nothing was recorded."""


def capture_item(macos, path: Path | str) -> CapturedItem:
    path = Path(path)
    # Sample, hash, sample again. `walk` raises SpecialEntry here, before any
    # subprocess is spawned.
    before = list(walk(path))
    hashes = {entry.rel_path: sha256_stream(_child(path, entry.rel_path))
              for entry in before if entry.entry_type == "file"}
    after = list(walk(path))
    if _fingerprints(before) != _fingerprints(after):
        raise CaptureAborted(f"{path} changed while it was being captured")

    kind = _kind(macos, path, before[0])
    entries = [_entry(macos, path, entry, hashes.get(entry.rel_path), kind)
               for entry in before]
    return CapturedItem(
        spool_path=str(path),
        item_name=path.name,
        kind=kind,
        root_sha256=(entries[0].sha256 if kind == "file"
                     else manifest_hash(entries)),
        size_bytes=sum(entry.size_bytes or 0 for entry in entries),
        entries=entries,
    )


def manifest_hash(entries) -> str:
    """SHA-256 over canonical NDJSON of the manifest fields, ordered by path."""
    digest = hashlib.sha256()
    for entry in sorted(entries, key=lambda item: item.rel_path):
        row = {field: getattr(entry, field) for field in MANIFEST_FIELDS}
        digest.update(json.dumps(row, sort_keys=True, ensure_ascii=False,
                                 separators=(",", ":")).encode("utf-8",
                                                               "surrogateescape"))
        digest.update(b"\n")
    return digest.hexdigest()


def _child(root: Path, rel_path: str) -> Path:
    return root / rel_path if rel_path else root


def _fingerprints(entries):
    return [(entry.rel_path, entry.entry_type, entry.size_bytes, entry.mtime_ns,
             entry.ctime_ns, entry.inode, entry.dev, entry.link_target)
            for entry in entries]


def _kind(macos, path: Path, root: WalkEntry) -> str:
    if root.entry_type != "dir":
        return "file"
    try:
        return "bundle" if macos.is_bundle(str(path)) else "dir"
    except CaptureFailure:
        # Unknown bundle status: treat it as a plain directory rather than
        # collapsing a tree into one logical item on a guess.
        return "dir"


def _entry(macos, root: Path, walk_entry: WalkEntry, sha256: str | None,
           item_kind: str) -> CapturedEntry:
    path = _child(root, walk_entry.rel_path)
    is_root = walk_entry.rel_path == ""
    entry = CapturedEntry(
        rel_path=walk_entry.rel_path,
        entry_type=walk_entry.entry_type,
        size_bytes=walk_entry.size_bytes,
        sha256=sha256,
        link_target=walk_entry.link_target,
        mode=walk_entry.mode,
        fingerprint=walk_entry,
        # A bundle is one logical item: its internals are archived but never
        # returned as independent search results.
        searchable=is_root or item_kind != "bundle",
    )

    failures: list[str] = []
    try:
        for key, value in macos.mdls(str(path)).items():
            entry.attributes[(key, "mdls")] = value
    except CaptureFailure:
        failures.append("mdls")
    try:
        importer = macos.importer_attributes(str(path))
        for key, value in importer.items():
            entry.attributes[(key, "importer")] = value
        text = importer.get("kMDItemTextContent")
        entry.text = (text.value if text is not None and
                      isinstance(text.value, str) and text.value else None)
    except CaptureFailure:
        failures.append("importer")
    try:
        entry.xattrs = macos.xattrs(str(path))
    except CaptureFailure:
        failures.append("xattr")
    try:
        entry.tags = macos.finder_tags(str(path))
        entry.comment = macos.finder_comment(str(path))
    except CaptureFailure:
        failures.append("finder")

    if failures:
        entry.capture_status = "partial:" + ",".join(sorted(set(failures)))
    return entry
