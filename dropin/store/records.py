"""Store writes and reads.

Identity is globally namespaced (`<32-hex store>.<ULID>`) so two stores can be
merged during recovery without collisions, and attempt sequences are unique per
origin store rather than globally — two machines may each legitimately hold
sequence 1.

Capture is one transaction: an occurrence with half its entries would be a lie
the rest of the pipeline would believe.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import base64
import json
import os
import secrets
import sqlite3
import time
from typing import Iterator

from ..capture.model import CapturedItem
from ..engine.interface import IDENTIFIER_RE, STORE_ID_RE, Identity
from ..pipeline.fingerprint import capture as capture_fingerprints
from ..store.db import transaction

CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_last_ulid_ms = 0


class IdentityCollision(Exception):
    """Two different things claim one identifier. Always fails closed."""


@dataclass(frozen=True)
class Attempt:
    attempt_id: str
    occ_id: str
    origin_store_id: str
    export_seq: int
    export_path: str


# ---- identity --------------------------------------------------------------

def new_store_id() -> str:
    return secrets.token_hex(16)


def new_id(store_id: str) -> str:
    """`<store>.<ULID>`: namespaced so a merge cannot alias two occurrences."""
    if not STORE_ID_RE.match(store_id):
        raise ValueError(f"store id must be 32 lowercase hex: {store_id!r}")
    return f"{store_id}.{_ulid()}"


def _ulid() -> str:
    """Canonical uppercase Crockford ULID: 48-bit time, 80 bits of entropy."""
    global _last_ulid_ms
    milliseconds = int(time.time() * 1000)
    # Monotonic within a process so ids from one run sort in creation order.
    if milliseconds <= _last_ulid_ms:
        milliseconds = _last_ulid_ms + 1
    _last_ulid_ms = milliseconds
    value = (milliseconds << 80) | int.from_bytes(os.urandom(10), "big")
    digits = []
    for _ in range(26):
        digits.append(CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(digits))


def new_run_id() -> str:
    """A run id: a bare ULID, sortable by start time."""
    return _ulid()


def validate_id(value: str, store_id: str | None = None) -> str:
    if not IDENTIFIER_RE.match(value or ""):
        raise ValueError(f"not a namespaced identifier: {value!r}")
    if store_id is not None and not value.startswith(store_id + "."):
        raise ValueError(f"identifier {value!r} is not in store {store_id}")
    return value


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---- store metadata --------------------------------------------------------

def initialise_store(connection, store_id: str | None = None) -> str:
    store_id = store_id or new_store_id()
    with transaction(connection):
        connection.execute(
            "INSERT INTO store_meta (store_id, export_seq, published_frontier,"
            " created_at) VALUES (?, 0, 0, ?)", (store_id, _now()))
    return store_id


def store_meta(connection) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM store_meta").fetchone()
    if row is None:
        raise LookupError("store has no store_meta row; run `dropin init`")
    return row


def advance_frontier(connection, export_seq: int) -> None:
    """The frontier is a high-water mark of *confirmed* sequences only."""
    connection.execute(
        "UPDATE store_meta SET published_frontier = max(published_frontier, ?)",
        (export_seq,))


def note_observed_sequence(connection, export_seq: int) -> None:
    connection.execute(
        "UPDATE store_meta SET export_seq = max(export_seq, ?)", (export_seq,))


def merge_lineage(connection, store_id: str, merged_through_seq: int) -> None:
    connection.execute(
        "INSERT INTO lineage (store_id, adopted_at, merged_through_seq)"
        " VALUES (?,?,?) ON CONFLICT (store_id) DO UPDATE SET"
        " merged_through_seq = max(merged_through_seq, excluded.merged_through_seq)",
        (store_id, _now(), merged_through_seq))


def lineage(connection) -> dict[str, int]:
    return {row["store_id"]: row["merged_through_seq"]
            for row in connection.execute("SELECT * FROM lineage")}


# ---- occurrences -----------------------------------------------------------

def record_occurrence(connection, item: CapturedItem, store_id: str,
                      occ_id: str | None = None) -> str:
    occ_id = occ_id or new_id(store_id)
    validate_id(occ_id, store_id)
    archive_path = f"{occ_id}/{item.item_name}"
    duplicate = find_by_root_hash(connection, item.root_sha256)
    dedup_of = duplicate[0]["occ_id"] if duplicate else None

    with transaction(connection):
        connection.execute(
            "INSERT INTO occurrence (occ_id, origin_store_id, item_name,"
            " archive_path, kind, spool_path, root_sha256, size_bytes,"
            " entry_count, state, recorded_at, dedup_of)"
            " VALUES (?,?,?,?,?,?,?,?,?,'recorded',?,?)",
            (occ_id, store_id, item.item_name, archive_path, item.kind,
             item.spool_path, item.root_sha256, item.size_bytes,
             item.entry_count, _now(), dedup_of))
        for entry in item.entries:
            entry_path = (f"{archive_path}/{entry.rel_path}" if entry.rel_path
                          else archive_path)
            connection.execute(
                "INSERT INTO entry (occ_id, rel_path, entry_type, size_bytes,"
                " sha256, link_target, mode, archive_path, searchable,"
                " capture_status) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (occ_id, entry.rel_path, entry.entry_type, entry.size_bytes,
                 entry.sha256, entry.link_target, entry.mode, entry_path,
                 1 if entry.searchable else 0, entry.capture_status))
            for (key, source), value in entry.attributes.items():
                connection.execute(
                    "INSERT INTO attribute (occ_id, rel_path, key, value_json,"
                    " value_type, source) VALUES (?,?,?,?,?,?)",
                    (occ_id, entry.rel_path, key,
                     json.dumps(value.value, ensure_ascii=False), value.type,
                     source))
            for name, xattr in entry.xattrs.items():
                connection.execute(
                    "INSERT INTO xattr (occ_id, rel_path, name, value_b64,"
                    " status) VALUES (?,?,?,?,?)",
                    (occ_id, entry.rel_path, name,
                     None if xattr.value is None else
                     base64.b64encode(xattr.value).decode("ascii"),
                     xattr.status))
            if entry.searchable:
                connection.execute(
                    "INSERT INTO normalized (occ_id, rel_path, name, uti, kind,"
                    " created, modified, size_bytes, sha256, comment, text)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (occ_id, entry.rel_path,
                     entry.rel_path.rsplit("/", 1)[-1] or item.item_name,
                     entry.uti, entry.kind, entry.created, entry.modified,
                     entry.size_bytes, entry.sha256, entry.comment, entry.text))
            for tag in entry.tags:
                connection.execute(
                    "INSERT OR IGNORE INTO tag (occ_id, rel_path, tag)"
                    " VALUES (?,?,?)", (occ_id, entry.rel_path, tag))
        capture_fingerprints(connection, occ_id,
                             [entry.fingerprint for entry in item.entries])
    return occ_id


def get_occurrence(connection, occ_id: str) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM occurrence WHERE occ_id = ?",
                             (occ_id,)).fetchone()
    if row is None:
        raise LookupError(f"no such occurrence {occ_id}")
    return row


def find_by_root_hash(connection, root_sha256: str) -> list[sqlite3.Row]:
    return list(connection.execute(
        "SELECT * FROM occurrence WHERE root_sha256 = ? ORDER BY recorded_at,"
        " occ_id", (root_sha256,)))


def iter_entries(connection, occ_id: str) -> Iterator[sqlite3.Row]:
    cursor = connection.execute(
        "SELECT * FROM entry WHERE occ_id = ? ORDER BY rel_path", (occ_id,))
    while True:
        row = cursor.fetchone()
        if row is None:
            return
        yield row


def set_state(connection, occ_id: str, state: str, *, error: str | None = None,
              confirmed_attempt_id: str | None = None) -> None:
    column = {
        "recorded": "recorded_at", "transferred": "transferred_at",
        "verified": "verified_at", "recoverable": "recoverable_at",
        "evicting": "evicting_at", "evicted": "evicted_at",
        "abandoned": "abandoned_at",
    }[state]
    assignments = ["state = ?", "last_error = ?"]
    values: list = [state, error]
    # Returning to recorded after a failed attempt is not a new capture.
    if state != "recorded":
        assignments.append(f"{column} = ?")
        values.append(_now())
    if confirmed_attempt_id is not None:
        assignments.append("confirmed_attempt_id = ?")
        values.append(confirmed_attempt_id)
    values.append(occ_id)
    connection.execute(
        f"UPDATE occurrence SET {', '.join(assignments)} WHERE occ_id = ?",
        values)


def occurrences_in_state(connection, *states: str) -> list[sqlite3.Row]:
    placeholders = ",".join("?" for _ in states)
    return list(connection.execute(
        f"SELECT * FROM occurrence WHERE state IN ({placeholders})"
        " ORDER BY recorded_at, occ_id", states))


# ---- attempts --------------------------------------------------------------

def start_attempt(connection, occ_id: str, store_id: str, *, export_path: str,
                  export_seq: int | None = None) -> Attempt:
    """Committed *before* the backup runs, so a crash leaves evidence."""
    attempt_id = new_id(store_id)
    with transaction(connection):
        if export_seq is None:
            connection.execute(
                "UPDATE store_meta SET export_seq = export_seq + 1")
            export_seq = store_meta(connection)["export_seq"]
        else:
            note_observed_sequence(connection, export_seq)
        connection.execute(
            "INSERT INTO publication_attempt (attempt_id, origin_store_id,"
            " occ_id, export_seq, export_path, started_at, outcome)"
            " VALUES (?,?,?,?,?,?, 'pending')",
            (attempt_id, store_id, occ_id, export_seq, export_path, _now()))
    return Attempt(attempt_id, occ_id, store_id, export_seq, export_path)


def set_export_digest(connection, attempt_id: str, digest: str) -> None:
    connection.execute(
        "UPDATE publication_attempt SET export_sha256 = ? WHERE attempt_id = ?",
        (digest, attempt_id))


def set_attempt_snapshot(connection, attempt_id: str, snapshot_id: str) -> None:
    connection.execute(
        "UPDATE publication_attempt SET snapshot_id = ? WHERE attempt_id = ?",
        (snapshot_id, attempt_id))


def finish_attempt(connection, attempt_id: str, outcome: str,
                   reason: str | None = None) -> None:
    connection.execute(
        "UPDATE publication_attempt SET outcome = ?, reason = ?,"
        " finished_at = ? WHERE attempt_id = ?",
        (outcome, reason, _now(), attempt_id))


def get_attempt(connection, attempt_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM publication_attempt WHERE attempt_id = ?",
        (attempt_id,)).fetchone()
    if row is None:
        raise LookupError(f"no such attempt {attempt_id}")
    return row


def attempts_for(connection, occ_id: str) -> list[sqlite3.Row]:
    return list(connection.execute(
        "SELECT * FROM publication_attempt WHERE occ_id = ?"
        " ORDER BY started_at, export_seq", (occ_id,)))


def pending_attempts(connection) -> list[sqlite3.Row]:
    return list(connection.execute(
        "SELECT * FROM publication_attempt WHERE outcome = 'pending'"
        " ORDER BY started_at, export_seq"))


# ---- snapshot observation ledger -------------------------------------------

def observe_snapshot(connection, snapshot_id: str, identity: Identity, *,
                     status: str, reason: str | None = None) -> None:
    """Record exactly what the repository showed us.

    The ledger is what lets a later frontier check recognise a snapshot whose
    export was corrupt or missing, instead of calling the whole store stale.
    """
    tag_set = json.dumps(identity.tag_set(), sort_keys=True)
    existing = connection.execute(
        "SELECT * FROM snapshot WHERE snapshot_id = ?", (snapshot_id,)).fetchone()
    if existing is not None:
        if existing["tag_set_json"] != tag_set:
            raise IdentityCollision(
                f"snapshot {snapshot_id} was observed with different tags")
        connection.execute(
            "UPDATE snapshot SET status = ?, reason = ?, seen_at = ?"
            " WHERE snapshot_id = ?", (status, reason, _now(), snapshot_id))
        return
    connection.execute(
        "INSERT INTO snapshot (snapshot_id, occ_id, attempt_id, store_id,"
        " export_seq, kind, catalog_sha256, tag_set_json, status, reason,"
        " seen_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (snapshot_id, identity.occ_id, identity.attempt_id, identity.store_id,
         identity.export_seq, identity.kind, identity.catalog_sha256, tag_set,
         status, reason, _now()))


def get_snapshot(connection, snapshot_id: str) -> sqlite3.Row | None:
    return connection.execute("SELECT * FROM snapshot WHERE snapshot_id = ?",
                              (snapshot_id,)).fetchone()


def snapshots_for_attempt(connection, attempt_id: str) -> list[sqlite3.Row]:
    return list(connection.execute(
        "SELECT * FROM snapshot WHERE attempt_id = ? ORDER BY snapshot_id",
        (attempt_id,)))


def set_snapshot_status(connection, snapshot_id: str, status: str,
                        reason: str | None = None) -> None:
    connection.execute(
        "UPDATE snapshot SET status = ?, reason = ? WHERE snapshot_id = ?",
        (status, reason, snapshot_id))
