"""Remote catalog verification.

The recoverability gate. Before an original may be deleted, the catalog export is
dumped back *from the exact snapshot* and proven:

1. the dump succeeds and the stream is complete;
2. its SHA-256 equals the immutable `dropin:catalog-sha256` tag — and, for a
   running store, the live attempt's digest as well;
3. it opens read-only and `PRAGMA integrity_check` returns ok;
4. its single `export_lineage` row equals the snapshot's identity tags;
5. the occurrence row it carries matches the local one;
6. its entry rows equal the local manifest.

Order matters: the file is hashed before it is opened, because opening an
unverified SQLite file is the thing the digest exists to prevent.
"""

from __future__ import annotations

from contextlib import closing
import hashlib
import os
from pathlib import Path
import sqlite3

from ..engine.interface import Identity
from ..store import records

CHUNK_SIZE = 1024 * 1024


class CatalogError(Exception):
    """The remote catalog did not prove itself. Names the failed check."""

    def __init__(self, check: str, detail: str = "") -> None:
        super().__init__(f"catalog: {check}" + (f": {detail}" if detail else ""))
        self.check = check
        self.detail = detail


def verify_catalog(*, engine, snapshot_id: str, export_path: str,
                   identity: Identity, connection, occ_id: str,
                   tmp_dir: Path | str, live_digest: str | None) -> None:
    tmp_dir = Path(tmp_dir)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    dumped = tmp_dir / f"catalog-{identity.attempt_id}.sqlite"
    try:
        _dump(engine, snapshot_id, export_path, dumped)
        if live_digest is not None and live_digest != identity.catalog_sha256:
            raise CatalogError("digest",
                               "the live attempt and the snapshot tag disagree")
        verify_catalog_file(dumped, identity)
        _compare_rows(dumped, connection, occ_id)
    finally:
        try:
            os.unlink(dumped)
        except FileNotFoundError:
            pass


def verify_catalog_file(path: Path | str, identity: Identity) -> None:
    """Checks 2-4: everything provable from the file and its tags alone.

    Fresh recovery has no live store, so this is its entire catalog gate.
    """
    path = Path(path)
    digest = _sha256(path)
    if digest != identity.catalog_sha256:
        raise CatalogError("digest",
                           f"expected {identity.catalog_sha256}, read {digest}")
    try:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            result = db.execute("PRAGMA integrity_check").fetchone()[0]
            if result != "ok":
                raise CatalogError("integrity_check", result)
            rows = db.execute(
                "SELECT store_id, export_seq, occ_id, attempt_id"
                " FROM export_lineage").fetchall()
    except sqlite3.DatabaseError as error:
        raise CatalogError("open", str(error)) from error
    if len(rows) != 1:
        raise CatalogError("lineage", f"{len(rows)} lineage rows, expected 1")
    row = rows[0]
    expected = (identity.store_id, identity.export_seq, identity.occ_id,
                identity.attempt_id)
    if tuple(row) != expected:
        raise CatalogError("lineage",
                           f"row {tuple(row)} does not match tags {expected}")


def _compare_rows(path: Path, connection, occ_id: str) -> None:
    local = records.get_occurrence(connection, occ_id)
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        exported = db.execute("SELECT * FROM occurrence WHERE occ_id = ?",
                              (occ_id,)).fetchone()
        if exported is None:
            raise CatalogError("occurrence", f"{occ_id} is not in the export")
        for field in ("root_sha256", "kind", "entry_count", "item_name",
                      "archive_path"):
            if exported[field] != local[field]:
                raise CatalogError(
                    "occurrence",
                    f"{field} differs: local {local[field]!r}, "
                    f"export {exported[field]!r}")
        exported_entries = db.execute(
            "SELECT rel_path, entry_type, sha256, link_target FROM entry"
            " WHERE occ_id = ? ORDER BY rel_path", (occ_id,)).fetchall()

    local_entries = [
        (row["rel_path"], row["entry_type"], row["sha256"], row["link_target"])
        for row in records.iter_entries(connection, occ_id)]
    if len(exported_entries) != len(local_entries):
        raise CatalogError(
            "entries",
            f"{len(exported_entries)} exported, {len(local_entries)} local")
    for exported_row, local_row in zip(exported_entries, local_entries):
        if tuple(exported_row) != local_row:
            raise CatalogError("entries",
                               f"{local_row[0] or '.'} differs from the export")


def _dump(engine, snapshot_id: str, export_path: str, target: Path) -> None:
    try:
        with engine.dump(snapshot_id, export_path) as stream:
            with open(target, "wb") as handle:
                for chunk in iter(lambda: stream.read(CHUNK_SIZE), b""):
                    handle.write(chunk)
    except Exception as error:  # engine failure or truncated stream
        raise CatalogError("dump", str(error)) from error


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()
