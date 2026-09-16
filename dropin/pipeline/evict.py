"""Journalled deletion of a local original.

This is the only code in the project that destroys user data, so it is built to
refuse rather than to succeed:

* gate (d) re-compares the full source fingerprint inside the transaction that
  writes the eviction intent;
* the ownership check must be *supported* and clear for the whole item before
  the intent, and re-proven on every later pass — capability is not a fact that
  survives a restart;
* deletion walks bottom-up through directory descriptors, so a path swapped
  above us cannot redirect an unlink, and symbolic links are removed as links;
* immediately before each file or symlink is unlinked, its own descriptor check
  must be clear and its full fingerprint must still match; immediately before a
  directory is removed, its directory-form check must be clear and its identity
  (inode, device, type) must match — its metadata legitimately changed as its
  children went;
* anything unexpected stops the pass and retains the remainder in `evicting`,
  which never becomes `abandoned`: a half-deleted tree must never be re-captured
  as a fresh occurrence.

The residual window is stated rather than hidden: a writer that opens an entry
after its final check and before its unlink is not detected. That interval is
per entry and as short as the code can make it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat

from ..macos.interface import Unsupported
from ..store import records
from ..store.db import transaction
from .faults import fault_after
from .fingerprint import SourceChanged, compare
from ..spool.walk import walk

FINGERPRINT_FIELDS = ("rel_path", "entry_type", "size_bytes", "mtime_ns",
                      "ctime_ns", "inode", "dev", "link_target")


class Retained(Exception):
    """The item was not deleted, and the reason is reportable."""


def begin_eviction(connection, occ_id: str, ownership, spool_path: str) -> None:
    """Gate (d), the ownership check, and the durable intent."""
    _require_capability(ownership)
    holders = _check(ownership, spool_path, is_dir=_is_dir(spool_path))
    if holders:
        raise Retained(f"open writer: pid {', '.join(str(p) for p in holders)}")

    with transaction(connection):
        # Inside the transaction: nothing may change between the last look and
        # the durable decision to delete.
        compare(connection, occ_id, walk(spool_path))
        fingerprint = [
            {field: row[field] for field in FINGERPRINT_FIELDS}
            for row in connection.execute(
                "SELECT rel_path, entry_type, size_bytes, mtime_ns, ctime_ns,"
                " inode, dev, link_target FROM fingerprint WHERE occ_id = ?"
                " ORDER BY rel_path", (occ_id,))]
        connection.execute(
            "INSERT OR REPLACE INTO eviction_intent (occ_id, intent_at,"
            " fingerprint_json, progress_json, recovered_without_local_history)"
            " VALUES (?, datetime('now'), ?, NULL, 0)",
            (occ_id, json.dumps(fingerprint)))
        records.set_state(connection, occ_id, "evicting")


def run_eviction(connection, occ_id: str, ownership, spool_path: str) -> None:
    """Perform (or resume) the journalled deletion."""
    intent = connection.execute(
        "SELECT * FROM eviction_intent WHERE occ_id = ?", (occ_id,)).fetchone()
    if intent is None:
        raise Retained("no eviction intent; refusing to delete anything")
    recovered = bool(intent["recovered_without_local_history"])
    entries = json.loads(intent["fingerprint_json"])
    by_path = {entry["rel_path"]: entry for entry in entries}

    # Every pass, not just the first: capability can be lost between runs.
    _require_capability(ownership)
    root_exists = os.path.lexists(spool_path)
    if not root_exists:
        if recovered:
            # Recovery reconstructed this intent from the repository, so an
            # absent root is not evidence that *we* deleted it.
            raise Retained("retained: manual intervention (recovery-marked "
                           "intent whose root is absent)")
        _finish(connection, occ_id)
        return

    holders = _check(ownership, spool_path, is_dir=_is_dir(spool_path))
    if holders:
        raise Retained(f"open writer: pid {', '.join(str(p) for p in holders)}")

    for rel_path in sorted(by_path, key=_depth_first, reverse=True):
        if rel_path == "":
            continue
        if _remove(ownership, spool_path, rel_path, by_path[rel_path]):
            fault_after("mid-deletion")

    _remove_root(ownership, spool_path, by_path[""])
    fault_after("root-removed")
    _finish(connection, occ_id)


def _finish(connection, occ_id: str) -> None:
    with transaction(connection):
        connection.execute("DELETE FROM eviction_intent WHERE occ_id = ?",
                           (occ_id,))
        records.set_state(connection, occ_id, "evicted")


def _depth_first(rel_path: str) -> tuple[int, str]:
    return (rel_path.count("/"), rel_path)


def _require_capability(ownership) -> None:
    capabilities = ownership.capabilities()
    if not capabilities.ownership_check:
        raise Retained(
            f"ownership check unavailable: {capabilities.ownership_reason}")


def _check(ownership, path: str, *, is_dir: bool) -> list[int]:
    try:
        return ownership.open_descriptors(path, is_dir)
    except Unsupported as error:
        raise Retained(f"ownership check unavailable: {error}") from error


def _is_dir(path: str) -> bool:
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


def _remove(ownership, spool_path: str, rel_path: str, expected: dict) -> bool:
    """Remove one entry; True when this call removed it."""
    path = os.path.join(spool_path, rel_path)
    parent = os.path.dirname(path)
    name = os.path.basename(path)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        # Absent under a live intent: this intent's own earlier pass removed it.
        return False
    is_dir = stat.S_ISDIR(info.st_mode)

    holders = _check(ownership, path, is_dir=is_dir)
    if holders:
        raise Retained(
            f"{rel_path}: open writer pid "
            f"{', '.join(str(pid) for pid in holders)}")
    _compare_entry(rel_path, path, info, expected, is_dir)

    # Directory descriptors: the parent is opened once and the removal is
    # relative to it, so a rename above us cannot redirect the unlink.
    fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if is_dir:
            _require_empty(path, rel_path)
            os.rmdir(name, dir_fd=fd)
        else:
            os.unlink(name, dir_fd=fd)
    except OSError as error:
        raise Retained(f"{rel_path}: {error.strerror or error}") from error
    finally:
        os.close(fd)
    return True


def _remove_root(ownership, spool_path: str, expected: dict) -> None:
    try:
        info = os.lstat(spool_path)
    except FileNotFoundError:
        return
    is_dir = stat.S_ISDIR(info.st_mode)
    holders = _check(ownership, spool_path, is_dir=is_dir)
    if holders:
        raise Retained(
            f"open writer pid {', '.join(str(pid) for pid in holders)}")
    _compare_entry("", spool_path, info, expected, is_dir)
    parent = os.path.dirname(spool_path.rstrip("/"))
    name = os.path.basename(spool_path.rstrip("/"))
    fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if is_dir:
            _require_empty(spool_path, "")
            os.rmdir(name, dir_fd=fd)
        else:
            os.unlink(name, dir_fd=fd)
    except OSError as error:
        raise Retained(f"{error.strerror or error}") from error
    finally:
        os.close(fd)


def _require_empty(path: str, rel_path: str) -> None:
    leftover = sorted(os.listdir(path))
    if leftover:
        raise Retained(
            f"{rel_path or '.'}: unexpected entry {leftover[0]!r} remains")


def _compare_entry(rel_path: str, path: str, info, expected: dict,
                   is_dir: bool) -> None:
    if is_dir:
        # A directory's size and timestamps change as its children go; only its
        # identity can be compared this late.
        if (info.st_ino, info.st_dev) != (expected["inode"], expected["dev"]) \
                or expected["entry_type"] != "dir":
            raise Retained(f"{rel_path or '.'}: directory identity changed")
        return

    entry_type = "symlink" if stat.S_ISLNK(info.st_mode) else "file"
    if entry_type != expected["entry_type"]:
        raise Retained(f"{rel_path or '.'}: type changed since the intent")
    actual = {
        "size_bytes": info.st_size if entry_type == "file" else None,
        "mtime_ns": info.st_mtime_ns,
        "ctime_ns": info.st_ctime_ns,
        "inode": info.st_ino,
        "dev": info.st_dev,
        "link_target": os.readlink(path) if entry_type == "symlink" else None,
    }
    for field in ("size_bytes", "mtime_ns", "ctime_ns", "inode", "dev",
                  "link_target"):
        if actual[field] != expected[field]:
            raise Retained(f"{rel_path or '.'}: {field} changed since the intent")
