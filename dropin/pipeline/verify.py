"""Payload verification: read the bytes back and hash them.

Never a structure-only check. For a file that is `dump` plus a hash; for a tree
it is one tar stream, hashed member by member, with the member set compared
against the expected manifest — because a truncated stream can end on a member
boundary and parse perfectly well with entries missing. Symlink targets
are compared here too: `ls --json` does not expose them.

Memory is bounded by the largest member, not by the entry count: `TarFile` keeps
every member it has seen unless told otherwise, which for a 20 000-file tree is
the difference between a few MB and hundreds.
"""

from __future__ import annotations

import hashlib
import tarfile

from ..capture.extract import MANIFEST_FIELDS
from ..store import records

CHUNK_SIZE = 1024 * 1024


class PayloadError(Exception):
    """Archived bytes do not match the record."""

    def __init__(self, rel_path: str, detail: str) -> None:
        super().__init__(f"{rel_path or '.'}: {detail}")
        self.rel_path = rel_path
        self.detail = detail


def verify_payload(engine, snapshot_id: str, occ_id: str, connection,
                   spool_path: str) -> None:
    occurrence = records.get_occurrence(connection, occ_id)
    if occurrence["kind"] == "file":
        _verify_file(engine, snapshot_id, connection, occ_id, spool_path)
    else:
        _verify_tree(engine, snapshot_id, connection, occ_id, spool_path)


def _verify_file(engine, snapshot_id: str, connection, occ_id: str,
                 spool_path: str) -> None:
    row = next(iter(records.iter_entries(connection, occ_id)))
    digest = hashlib.sha256()
    try:
        with engine.dump(snapshot_id, spool_path) as stream:
            for chunk in iter(lambda: stream.read(CHUNK_SIZE), b""):
                digest.update(chunk)
    except Exception as error:  # engine errors are corruption evidence
        raise PayloadError("", f"read failed: {error}") from error
    if digest.hexdigest() != row["sha256"]:
        raise PayloadError("", "content hash differs from the record")


def _verify_tree(engine, snapshot_id: str, connection, occ_id: str,
                 spool_path: str) -> None:
    # Each member is looked up by primary key as it arrives, and the paths seen
    # go into a temp table, so neither the manifest nor the member set is held
    # in memory: a tree with 20 000 entries must cost the same as one with 20.
    connection.execute("CREATE TEMP TABLE IF NOT EXISTS verify_seen "
                       "(rel_path TEXT PRIMARY KEY)")
    connection.execute("DELETE FROM verify_seen")
    prefix = spool_path.lstrip("/")
    select = ("SELECT rel_path, entry_type, size_bytes, sha256, link_target"
              " FROM entry WHERE occ_id = ? AND rel_path = ?")

    try:
        with engine.dump(snapshot_id, spool_path, archive="tar") as stream:
            with tarfile.open(fileobj=stream, mode="r|") as tar:
                for member in tar:
                    rel_path = _member_rel_path(member.name, prefix)
                    inserted = connection.execute(
                        "INSERT OR IGNORE INTO verify_seen VALUES (?)",
                        (rel_path,)).rowcount
                    if not inserted:
                        raise PayloadError(rel_path, "duplicate member in stream")
                    row = connection.execute(select, (occ_id, rel_path)).fetchone()
                    if row is None:
                        raise PayloadError(
                            rel_path, "member is in the stream but not the manifest")
                    _check_member(tar, member, row, rel_path)
                    # Without this the reader retains every member it has seen.
                    tar.members.clear()
    except tarfile.TarError as error:
        raise PayloadError("", f"tar stream unreadable: {error}") from error

    # The stream carries the tree's descendants, never the root itself
    # (confirmed against restic 0.19.1); the root node is proven by `ls` in
    # reconciliation, so it is not expected here.
    missing = connection.execute(
        "SELECT e.rel_path FROM entry e"
        " LEFT JOIN verify_seen s ON s.rel_path = e.rel_path"
        " WHERE e.occ_id = ? AND e.rel_path != '' AND s.rel_path IS NULL"
        " ORDER BY e.rel_path LIMIT 1", (occ_id,)).fetchone()
    connection.execute("DELETE FROM verify_seen")
    if missing is not None:
        raise PayloadError(missing["rel_path"], "member is missing from the stream")


def _member_rel_path(name: str, prefix: str) -> str:
    """Member names come from the stream and are never trusted as paths."""
    if name.startswith("/"):
        raise PayloadError(name, "absolute member name")
    parts = name.split("/")
    if ".." in parts or "." in parts:
        raise PayloadError(name, "member name contains a dot segment")
    if name == prefix:
        return ""
    if not name.startswith(prefix + "/"):
        raise PayloadError(name, "member is outside the item")
    return name[len(prefix) + 1:]


def _check_member(tar, member, row, rel_path: str) -> None:
    if member.issym():
        if row["entry_type"] != "symlink":
            raise PayloadError(rel_path, "member is a symlink, manifest is not")
        if member.linkname != row["link_target"]:
            raise PayloadError(
                rel_path,
                f"link target differs: manifest {row['link_target']!r}, "
                f"stream {member.linkname!r}")
        return
    if member.isdir():
        if row["entry_type"] != "dir":
            raise PayloadError(rel_path, "member is a directory, manifest is not")
        return
    if member.islnk():
        # restic never emits hardlink members; if one appears we are not
        # reading the stream we think we are.
        raise PayloadError(rel_path, "unexpected hardlink member")
    if not member.isreg():
        raise PayloadError(rel_path, f"unsupported member type {member.type!r}")
    if row["entry_type"] != "file":
        raise PayloadError(rel_path, "member is a regular file, manifest is not")

    handle = tar.extractfile(member)
    if handle is None:
        raise PayloadError(rel_path, "member has no readable content")
    digest = hashlib.sha256()
    for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
        digest.update(chunk)
    if digest.hexdigest() != row["sha256"]:
        raise PayloadError(rel_path, "content hash differs from the record")


def recompute_manifest_hash(connection, occ_id: str) -> str:
    """Rebuild the expected-manifest hash from the stored rows."""
    import json

    digest = hashlib.sha256()
    for row in records.iter_entries(connection, occ_id):
        payload = {field: row[field if field != "rel_path" else "rel_path"]
                   for field in MANIFEST_FIELDS}
        digest.update(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                 separators=(",", ":")).encode("utf-8",
                                                               "surrogateescape"))
        digest.update(b"\n")
    return digest.hexdigest()
