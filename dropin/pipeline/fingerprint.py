"""Source-stability capture and gates.

The fingerprint is the archiver's answer to "is this still the item I looked
at?" It is sampled before hashing and again after, then re-compared immediately
before backup, after backup, before verification is accepted, and inside the
transaction that writes the eviction intent. Any difference at all is a
refusal — including a metadata-only one, because we cannot tell a harmless
touch from a rewrite that happens to keep the size.

The comparison iterates both sides with a cursor: an item with a million
entries must not become a million-row list in memory.
"""

from __future__ import annotations

from typing import Iterable, Iterator

FIELDS = ("entry_type", "size_bytes", "mtime_ns", "ctime_ns", "inode", "dev",
          "link_target")


class SourceChanged(Exception):
    """The source differs from what was recorded. Names the entry."""

    def __init__(self, rel_path: str, detail: str = "") -> None:
        super().__init__(f"source changed at {rel_path or '.'}"
                         + (f": {detail}" if detail else ""))
        self.rel_path = rel_path
        self.detail = detail


def capture(connection, occ_id: str, entries: Iterable) -> None:
    """Store one fingerprint row per entry. Immutable after `recorded`."""
    connection.executemany(
        "INSERT INTO fingerprint (occ_id, rel_path, entry_type, size_bytes,"
        " mtime_ns, ctime_ns, inode, dev, link_target)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        ((occ_id, entry.rel_path, entry.entry_type, entry.size_bytes,
          entry.mtime_ns, entry.ctime_ns, entry.inode, entry.dev,
          entry.link_target) for entry in entries))


def compare(connection, occ_id: str, live: Iterable) -> None:
    """Raise `SourceChanged` unless the live walk matches the stored rows.

    Each live entry is checked by primary key as it arrives, so nothing is
    accumulated in memory; the paths seen go into a temp table purely so a
    *removed* entry can still be named exactly. Both directions of the
    difference are therefore attributable without materialising either side.
    """
    connection.execute("CREATE TEMP TABLE IF NOT EXISTS fingerprint_live "
                       "(rel_path TEXT PRIMARY KEY)")
    connection.execute("DELETE FROM fingerprint_live")
    select = (
        "SELECT entry_type, size_bytes, mtime_ns, ctime_ns, inode, dev,"
        " link_target FROM fingerprint WHERE occ_id = ? AND rel_path = ?")
    for entry in _iterate(live):
        row = connection.execute(select, (occ_id, entry.rel_path)).fetchone()
        if row is None:
            raise SourceChanged(entry.rel_path, "entry added")
        for field in FIELDS:
            if row[field] != getattr(entry, field):
                raise SourceChanged(entry.rel_path, f"{field} differs")
        connection.execute("INSERT INTO fingerprint_live VALUES (?)",
                           (entry.rel_path,))

    removed = connection.execute(
        "SELECT f.rel_path FROM fingerprint f"
        " LEFT JOIN fingerprint_live l ON l.rel_path = f.rel_path"
        " WHERE f.occ_id = ? AND l.rel_path IS NULL"
        " ORDER BY f.rel_path LIMIT 1", (occ_id,)).fetchone()
    connection.execute("DELETE FROM fingerprint_live")
    if removed is not None:
        raise SourceChanged(removed["rel_path"], "entry removed")


def _iterate(live: Iterable) -> Iterator:
    """A source that vanished or became unreadable is a changed source."""
    iterator = iter(live)
    while True:
        try:
            yield next(iterator)
        except StopIteration:
            return
        except OSError as error:
            raise SourceChanged("", f"source unreadable: {error}") from error
