"""The catalog export carried inside every snapshot.

This is what makes "remote plus password is enough" true. Sequence of events,
and the order matters:

1. allocate `export_seq` and commit a **pending** attempt with a null digest;
2. copy the whole store with SQLite's backup API — which includes committed but
   not yet checkpointed WAL data, unlike a file copy;
3. set the copy to DELETE journal mode, replace its lineage table with exactly
   one row identifying this attempt, close it;
4. rename it into place, hash the finalized file, and commit that digest to the
   *live* attempt row, where it becomes the immutable `dropin:catalog-sha256`
   snapshot tag.

The digest is deliberately not inside the export: a whole-file hash cannot
describe the file it lives in. Fresh recovery uses the tag as its expected hash
before it opens or trusts anything.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import sqlite3

from . import records

CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True)
class ExportResult:
    attempt_id: str
    export_seq: int
    export_path: str
    catalog_sha256: str


def export_catalog(connection, occ_id: str, store_id: str,
                   export_dir: Path | str) -> ExportResult:
    export_dir = Path(export_dir)
    export_dir.mkdir(parents=True, exist_ok=True)
    attempt = records.start_attempt(
        connection, occ_id, store_id,
        export_path=str(export_dir / "pending.sqlite"))
    final_path = export_dir / f"{occ_id}-{attempt.attempt_id}.sqlite"
    connection.execute(
        "UPDATE publication_attempt SET export_path = ? WHERE attempt_id = ?",
        (str(final_path), attempt.attempt_id))

    temporary = export_dir / f".{attempt.attempt_id}.building"
    _build(connection, temporary, store_id, attempt, occ_id)
    os.replace(temporary, final_path)
    _fsync_dir(export_dir)

    digest = _sha256(final_path)
    records.set_export_digest(connection, attempt.attempt_id, digest)
    return ExportResult(attempt.attempt_id, attempt.export_seq, str(final_path),
                        digest)


def _build(connection, path: Path, store_id: str, attempt, occ_id: str) -> None:
    if path.exists():
        path.unlink()
    with closing(sqlite3.connect(path)) as target:
        connection.backup(target)
        # Triggers on the copied schema would fight the lineage rewrite, and the
        # export is a snapshot of state, not a live store.
        target.execute("DELETE FROM export_lineage")
        target.execute(
            "INSERT INTO export_lineage (store_id, export_seq, occ_id,"
            " attempt_id, exported_at) VALUES (?,?,?,?,?)",
            (store_id, attempt.export_seq, occ_id, attempt.attempt_id,
             datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")))
        target.commit()
        # A standalone, closed artifact: no WAL sidecar to lose in transit.
        target.execute("PRAGMA journal_mode=DELETE")
    for sidecar in (f"{path}-wal", f"{path}-shm"):
        if os.path.exists(sidecar):
            os.unlink(sidecar)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_dir(path: Path) -> None:
    handle = os.open(path, os.O_RDONLY)
    try:
        os.fsync(handle)
    finally:
        os.close(handle)


def discard_export(path: str | Path) -> None:
    """Export files are disposable once the attempt is settled."""
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
