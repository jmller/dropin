"""Publication attempts.

One `restic backup` is one attempt, identified in the snapshot tags. Attempts
exist because backup is not idempotent: repeating it creates a second snapshot,
and snapshots are never deleted. So every attempt must be settled
exactly once, and a running store must never re-adopt an attempt it has already
marked failed — the snapshot may be partial, and "it exists" is not proof.

Failure is atomic: attempt failed, snapshot orphaned, occurrence back to
`recorded`. That way `transferred` and `verified` always imply a live pending
attempt, and later states always imply a confirmed one.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..engine.interface import Identity
from ..store import records
from ..store.db import transaction
from ..store.export import discard_export, export_catalog


class AttemptFailed(Exception):
    """This attempt is settled as failed and must never be resumed."""


@dataclass(frozen=True)
class StartedAttempt:
    attempt_id: str
    occ_id: str
    export_seq: int
    export_path: str
    catalog_sha256: str


@dataclass(frozen=True)
class ResumedAttempt:
    attempt_id: str
    snapshot_id: str | None
    reason: str | None = None


@dataclass(frozen=True)
class RetryState:
    decision: str  # ready | deferred | exhausted | confirmed
    reason: str = ""


def start(connection, occ_id: str, store_id: str, export_dir) -> StartedAttempt:
    """Allocate a sequence, build the export, and commit a pending attempt."""
    result = export_catalog(connection, occ_id, store_id, export_dir)
    return StartedAttempt(result.attempt_id, occ_id, result.export_seq,
                          result.export_path, result.catalog_sha256)


def identity_for(connection, attempt_id: str, kind: str) -> Identity:
    attempt = records.get_attempt(connection, attempt_id)
    return Identity(store_id=attempt["origin_store_id"],
                    occ_id=attempt["occ_id"],
                    attempt_id=attempt["attempt_id"],
                    export_seq=attempt["export_seq"], kind=kind,
                    catalog_sha256=attempt["export_sha256"])


def adopt(connection, attempt_id: str, snapshot_id: str,
          identity: Identity) -> None:
    """Bind a published snapshot to its attempt and ledger the observation."""
    with transaction(connection):
        records.set_attempt_snapshot(connection, attempt_id, snapshot_id)
        records.observe_snapshot(connection, snapshot_id, identity,
                                 status="pending")


def resume(connection, engine, attempt_id: str) -> ResumedAttempt:
    """Reconcile an attempt whose snapshot id was never recorded.

    The attempt row is committed before the backup runs, so a crash in between
    is expected, not exceptional: ask the repository which snapshots carry this
    attempt tag. None means nothing was published. Two mean we cannot tell which
    is ours, so both are orphaned and the attempt fails.
    """
    attempt = records.get_attempt(connection, attempt_id)
    if attempt["outcome"] == "failed":
        raise AttemptFailed(
            f"attempt {attempt_id} already failed: {attempt['reason']}")
    if attempt["snapshot_id"]:
        return ResumedAttempt(attempt_id, attempt["snapshot_id"])

    found = engine.snapshots(tag=f"dropin:attempt={attempt_id}")
    if not found:
        fail(connection, attempt_id, "no snapshot")
        return ResumedAttempt(attempt_id, None, "no snapshot")

    occurrence = records.get_occurrence(connection, attempt["occ_id"])
    identity = identity_for(connection, attempt_id, occurrence["kind"])
    if len(found) > 1:
        with transaction(connection):
            for snapshot in found:
                records.observe_snapshot(connection, snapshot.id, identity,
                                         status="orphaned", reason="duplicate")
            records.finish_attempt(connection, attempt_id, "failed",
                                   reason="duplicate")
            records.set_state(connection, attempt["occ_id"], "recorded",
                              error="duplicate")
        discard_export(attempt["export_path"])
        return ResumedAttempt(attempt_id, None, "duplicate")

    adopt(connection, attempt_id, found[0].id, identity)
    return ResumedAttempt(attempt_id, found[0].id)


def fail(connection, attempt_id: str, reason: str,
         snapshot_id: str | None = None) -> None:
    """Settle an attempt as failed, atomically, with its occurrence."""
    attempt = records.get_attempt(connection, attempt_id)
    snapshot_id = snapshot_id or attempt["snapshot_id"]
    with transaction(connection):
        records.finish_attempt(connection, attempt_id, "failed", reason=reason)
        if snapshot_id:
            records.set_snapshot_status(connection, snapshot_id, "orphaned",
                                        reason=reason)
        records.set_state(connection, attempt["occ_id"], "recorded",
                          error=reason)
    discard_export(attempt["export_path"])


def confirm(connection, attempt_id: str, occ_id: str, snapshot_id: str,
            export_seq: int) -> None:
    """All three proofs passed: this is the occurrence's single publication."""
    attempt = records.get_attempt(connection, attempt_id)
    with transaction(connection):
        records.finish_attempt(connection, attempt_id, "confirmed")
        records.set_snapshot_status(connection, snapshot_id, "confirmed")
        records.set_state(connection, occ_id, "recoverable",
                          confirmed_attempt_id=attempt_id)
        records.advance_frontier(connection, export_seq)
    discard_export(attempt["export_path"])


def abandon_source_changed(connection, occ_id: str, attempt_id: str, *,
                           snapshot_id: str | None, confirmed: bool) -> None:
    """The source changed. What happens to the attempt depends on when.

    Before confirmation (gates a-c) the attempt is worthless and fails. After
    confirmation (gate d) the publication is real and verified: preserving it
    keeps a recoverable copy of what the item *was*, while the occurrence is
    abandoned and the changed source is left alone.
    """
    reason = ("source changed after publication" if confirmed
              else "source changed")
    with transaction(connection):
        if not confirmed:
            records.finish_attempt(connection, attempt_id, "failed",
                                   reason="source changed")
            if snapshot_id:
                records.set_snapshot_status(connection, snapshot_id, "orphaned",
                                            reason="source changed")
        records.set_state(connection, occ_id, "abandoned", error=reason)
    if not confirmed:
        attempt = records.get_attempt(connection, attempt_id)
        discard_export(attempt["export_path"])


def retry_state(connection, occ_id: str, *, max_attempts: int,
                retry_backoff_seconds: float, now: float,
                retry_exhausted: bool = False) -> RetryState:
    """May this occurrence start a new attempt right now?

    Every attempt may leave an immutable snapshot the archiver will never
    delete, so a persistently failing item must stop trying rather than fill the
    repository one launchd interval at a time.
    """
    history = records.attempts_for(connection, occ_id)
    if any(row["outcome"] == "confirmed" for row in history):
        return RetryState("confirmed")
    failed = [row for row in history if row["outcome"] == "failed"]
    if failed:
        latest = max(failed, key=lambda row: row["finished_at"] or "")
        deadline = _epoch(latest["finished_at"]) + retry_backoff_seconds
        if now < deadline:
            return RetryState(
                "deferred",
                f"backoff: {int(deadline - now)}s until the next attempt")
    if len(failed) >= max_attempts and not retry_exhausted:
        return RetryState("exhausted",
                          f"attempts exhausted after {len(failed)} failures")
    return RetryState("ready")


def _epoch(timestamp: str | None) -> float:
    if not timestamp:
        return 0.0
    from datetime import datetime, timezone

    return datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc).timestamp()
