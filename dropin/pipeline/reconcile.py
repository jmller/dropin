"""Snapshot contents versus the expected manifest.

A snapshot's existence proves nothing: restic exit 3 publishes a snapshot with
entries missing. Reconciliation is the first of the three proofs an attempt must
pass, and it compares exactly what `ls --json` provides — path, type, and size
for regular files. Link targets are *not* here: 0.19.1 exposes no `linktarget`
on ls nodes, so they are proven from the tar member `linkname` during
payload verification instead.
"""

from __future__ import annotations

from ..store import records

#: What an `ls --json` node can actually tell us about an entry.
COMPARED_FIELDS = ("entry_type", "size_bytes")


class ReconcileError(Exception):
    """The snapshot does not contain exactly the expected manifest."""

    def __init__(self, rel_path: str, detail: str) -> None:
        super().__init__(f"{rel_path or '.'}: {detail}")
        self.rel_path = rel_path
        self.detail = detail


def reconcile(engine, snapshot_id: str, occ_id: str, connection,
              spool_path: str) -> None:
    expected = {}
    for row in records.iter_entries(connection, occ_id):
        expected[_absolute(spool_path, row["rel_path"])] = row

    seen: set[str] = set()
    for node in engine.ls(snapshot_id, spool_path):
        row = expected.get(node.path)
        if row is None:
            raise ReconcileError(_relative(spool_path, node.path),
                                 "entry is in the snapshot but not the manifest")
        seen.add(node.path)
        if node.type != row["entry_type"]:
            raise ReconcileError(
                row["rel_path"],
                f"type differs: manifest {row['entry_type']}, snapshot {node.type}")
        if row["entry_type"] == "file" and node.size != row["size_bytes"]:
            raise ReconcileError(
                row["rel_path"],
                f"size differs: manifest {row['size_bytes']}, snapshot {node.size}")

    for path, row in expected.items():
        if path not in seen:
            raise ReconcileError(row["rel_path"], "entry is missing from the snapshot")


def _absolute(spool_path: str, rel_path: str) -> str:
    return f"{spool_path}/{rel_path}" if rel_path else spool_path


def _relative(spool_path: str, absolute: str) -> str:
    if absolute == spool_path:
        return ""
    prefix = spool_path.rstrip("/") + "/"
    return absolute[len(prefix):] if absolute.startswith(prefix) else absolute
