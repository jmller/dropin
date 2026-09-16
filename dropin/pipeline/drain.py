"""The drain driver: the per-occurrence state machine, run end to end.

Three passes, in this order, under one writer lock:

1. the current top-level spool scan, in name order — capture what is new,
   resume what is known, and record every occurrence id visited or reported;
2. one store-driven pass over pending eviction intents, excluding those ids,
   so an item is attempted and reported at most once per run;
3. one observation-only pass over the pending attempts of occurrences neither
   earlier pass reached (their spool item is gone, so no resume can run): an
   attempt with no snapshot is failed, one with snapshots is ledgered and left
   pending. This pass touches no source and reports no per-item outcome.

Failure scope is per item: each item's outcome is settled by its own
transactions, and nothing here rolls back or blocks another item. The only
whole-run refusals are the ones raised before the first item: a held writer
lock, and the frontier/lineage gate (which is also where an unreachable
repository is discovered).

`begin_eviction` is looked up on this module at call time on purpose: tests
patch it to mutate the source between the last verification and gate (d).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import resource
import shutil
import sys
import time
from typing import Callable

from ..capture.extract import CaptureAborted, capture_item
from ..engine.interface import EngineError, TagError, parse_tags
from ..progress import ProgressEvent
from ..report import Outcome, Report
from ..spool.admission import admit
from ..spool.scan import scan
from ..spool.walk import SpecialEntry, walk
from ..store import records
from ..store.db import transaction
from . import attempts
from .catalog_verify import CatalogError, verify_catalog
from .evict import Retained, begin_eviction, run_eviction  # noqa: F401
from .faults import fault_after
from .fingerprint import SourceChanged, compare
from .frontier import LineageMismatch, StaleStore, check_frontier
from .reconcile import ReconcileError, reconcile
from .verify import PayloadError, recompute_manifest_hash, verify_payload
from .writer_lock import LockHeld, writer_lock

ACTIVE_STATES = ("recorded", "transferred", "verified", "recoverable", "evicting")


@dataclass(frozen=True)
class DrainOptions:
    retry_exhausted: bool = False
    settle_seconds: float | None = None
    #: Added to the wall clock for retry decisions; tests use it to move past
    #: a backoff window without sleeping.
    now_offset: float = 0.0
    #: Overrides `tools.cache_max_mb` when set.
    cache_max_bytes: int | None = None
    #: Observational phase events; failures are ignored and grant no authority.
    progress: Callable[[ProgressEvent], None] | None = None


class _Stop(Exception):
    """This item's run ends here, with a reportable outcome."""

    def __init__(self, outcome: Outcome, reason: str | None = None) -> None:
        super().__init__(reason or outcome.value)
        self.outcome = outcome
        self.reason = reason


# ---- entry point -----------------------------------------------------------

def drain(context, report: Report, options: DrainOptions | None = None, *,
          lock: bool = True) -> Report:
    """Run one drain. `lock=False` only when the caller already holds the
    state directory's writer lock (the CLI dispatcher does)."""
    options = options or DrainOptions()
    if not lock:
        _Run(context, report, options, os.getpid()).execute()
        return report
    try:
        with writer_lock(context.config.writer_lock_path, verb="drain") as held:
            _Run(context, report, options, held["pid"]).execute()
    except LockHeld as error:
        report.run_refusal = f"locked: {error}"
    return report


class _Run:
    def __init__(self, context, report: Report, options: DrainOptions,
                 lock_pid: int) -> None:
        self.context = context
        self.config = context.config
        self.db = context.db
        self.engine = context.engine
        self.report = report
        self.options = options
        self.lock_pid = lock_pid
        self.store_id = records.store_meta(self.db)["store_id"]
        self.visited: set[str] = set()
        self.cache_evicted = False
        self.item_started = False
        self.item_index = 0
        self.item_total = 0
        self.completed_items = 0
        self.item_name: str | None = None

    # ---- the run -----------------------------------------------------------

    def execute(self) -> None:
        self._notify("prepare", "Preparing archive")
        self._open_run()
        try:
            self._enforce_ceilings()
            self._notify("repository", "Checking repository")
            try:
                check_frontier(self.db, self.engine, self.store_id)
            except (StaleStore, LineageMismatch) as error:
                self.report.run_refusal = str(error)
                return
            except EngineError as error:
                reason = f"repository: {_engine_reason(error)}"
                self.report.run_refusal = reason
                try:
                    paths = scan(self.config.drop_dir)
                except OSError as scan_error:
                    self.report.run_refusal += f"; spool scan failed: {scan_error}"
                else:
                    for path in paths:
                        self._emit(Outcome.REFUSED, path.name, reason=reason)
                return

            self._notify("scan", "Scanning drop folder")
            paths = list(scan(self.config.drop_dir))
            self.item_total = len(paths)
            for index, path in enumerate(paths, 1):
                self.item_started = True
                self.item_index = index
                self.item_name = path.name
                self._spool_item(path)
                self.completed_items += 1
            self._intent_pass()
            self._dormant_attempt_pass()
        finally:
            self.item_name = None
            self._notify("finalize", "Finalizing drain")
            self._close_run()

    def _notify(self, phase: str, label: str) -> None:
        callback = self.options.progress
        if callback is None:
            return
        event = ProgressEvent(
            phase, label, self.item_name,
            self.item_index if self.item_name and self.item_index else None,
            self.item_total if self.item_total else None,
            self.completed_items if self.item_total else None)
        try:
            callback(event)
        except Exception:
            # Presentation cannot influence archival state or deletion authority.
            pass

    def _open_run(self) -> None:
        # Test harnesses reuse a run id across runs of one store; a replaced
        # row is preferable to refusing to start.
        with transaction(self.db):
            self.db.execute(
                "INSERT OR REPLACE INTO run (run_id, verb, started_at,"
                " writer_lock_pid, cache_evicted) VALUES (?,?,?,?,0)",
                (self.report.run_id, self.report.verb, _now_text(),
                 self.lock_pid))

    def _close_run(self) -> None:
        usage = resource.getrusage(resource.RUSAGE_CHILDREN)
        peak_kb = usage.ru_maxrss // 1024 if sys.platform == "darwin" \
            else usage.ru_maxrss
        with transaction(self.db):
            self.db.execute(
                "UPDATE run SET finished_at = ?, exit_code = ?,"
                " cache_evicted = ?, restic_peak_rss_kb = ? WHERE run_id = ?",
                (_now_text(), self.report.exit_code(),
                 1 if self.cache_evicted else 0, peak_kb, self.report.run_id))

    # ---- ceilings -----------------------------------------------------

    def _enforce_ceilings(self) -> None:
        _empty_directory(self.config.tmp_dir)
        limit = self.options.cache_max_bytes
        if limit is None:
            limit = self.config.cache_max_mb * 1024 * 1024
        size = _tree_size(self.config.cache_dir)
        if size > limit:
            _empty_directory(self.config.cache_dir)
            self.cache_evicted = True
            self._emit(Outcome.INFO, "cache",
                       reason=f"cache emptied: {size} bytes exceeded the "
                              f"{limit}-byte ceiling; restic will rebuild it")

    # ---- pass 1: the spool scan --------------------------------------------

    def _spool_item(self, path: Path) -> None:
        spool_path = str(path)
        occurrence = self._active_occurrence(spool_path)
        if occurrence is None:
            occ_id = self._capture(path)
            if occ_id is None:
                return
        else:
            occ_id = occurrence["occ_id"]
        self.visited.add(occ_id)
        self._advance(occ_id, spool_path, path.name)

    def _active_occurrence(self, spool_path: str):
        placeholders = ",".join("?" for _ in ACTIVE_STATES)
        return self.db.execute(
            f"SELECT * FROM occurrence WHERE spool_path = ?"
            f" AND state IN ({placeholders})"
            f" ORDER BY recorded_at DESC, occ_id DESC LIMIT 1",
            (spool_path, *ACTIVE_STATES)).fetchone()

    def _capture(self, path: Path) -> str | None:
        name = path.name
        self._notify("stability", "Checking item stability")
        settle = self.options.settle_seconds
        if settle is None:
            settle = self.config.settle_seconds
        if not admit(path, settle_seconds=settle,
                     sample_gap_seconds=self.config.sample_gap_seconds):
            # Admission answers only "is it quiet?"; a walk tells a special
            # entry (a refusal) apart from churn (try again later).
            try:
                for _ in walk(path):
                    pass
            except SpecialEntry as error:
                self._emit(Outcome.REFUSED, name,
                           reason=f"unsupported entry: {error}")
                return None
            except OSError as error:
                self._emit(Outcome.REFUSED, name,
                           reason=f"unreadable: {error.strerror or error}")
                return None
            self._emit(Outcome.DEFERRED, name,
                       reason="not quiescent: the item is still changing or "
                              "newer than the settle window")
            return None

        self._notify("capture", "Capturing metadata")
        try:
            item = capture_item(self.context.macos, path)
        except SpecialEntry as error:
            self._emit(Outcome.REFUSED, name, reason=f"unsupported entry: {error}")
            return None
        except CaptureAborted:
            self._emit(Outcome.DEFERRED, name,
                       reason="source changed while it was being captured")
            return None
        except OSError as error:
            self._emit(Outcome.REFUSED, name,
                       reason=f"unreadable: {error.strerror or error}")
            return None
        occ_id = records.record_occurrence(self.db, item, self.store_id)
        fault_after("recorded")
        return occ_id

    # ---- pass 2: pending eviction intents ----------------------------------

    def _intent_pass(self) -> None:
        rows = self.db.execute(
            "SELECT o.* FROM eviction_intent i JOIN occurrence o"
            " ON o.occ_id = i.occ_id ORDER BY o.item_name, o.occ_id").fetchall()
        pending = [row for row in rows if row["occ_id"] not in self.visited
                   and row["state"] == "evicting"]
        self.item_total += len(pending)
        for occurrence in pending:
            occ_id = occurrence["occ_id"]
            self.visited.add(occ_id)
            self.item_started = True
            self.item_index += 1
            self.item_name = occurrence["item_name"]
            self._notify("reconcile", "Resuming interrupted removal")
            self._advance(occ_id, occurrence["spool_path"],
                          occurrence["item_name"])
            self.completed_items += 1

    # ---- pass 3: dormant pending attempts ----------------------

    def _dormant_attempt_pass(self) -> None:
        self.item_name = None
        pending = [attempt for attempt in records.pending_attempts(self.db)
                   if attempt["occ_id"] not in self.visited]
        if pending:
            self._notify("reconcile", "Reconciling interrupted uploads")
        for attempt in pending:
            if attempt["occ_id"] in self.visited:
                continue
            attempt_id = attempt["attempt_id"]
            try:
                found = self.engine.snapshots(tag=f"dropin:attempt={attempt_id}")
            except EngineError as error:
                # No observation was made: do not settle this attempt, emit an
                # item outcome, or prevent independent lookups from proceeding.
                reason = f"pending attempt {attempt_id}: {_engine_reason(error)}"
                field = "run_error" if self.item_started else "run_refusal"
                previous = getattr(self.report, field)
                setattr(self.report, field, f"{previous}\n{reason}" if previous else reason)
                continue
            if not found:
                # Nothing exists in the repository, so nothing is lost.
                attempts.fail(self.db, attempt_id, "no snapshot")
                continue
            # Ledger exactly what the repository shows; the attempt stays
            # pending so fresh recovery may still confirm it.
            with transaction(self.db):
                for snapshot in found:
                    try:
                        identity = parse_tags(snapshot.tags)
                    except TagError:
                        continue  # the frontier gate has already ruled on it
                    records.observe_snapshot(self.db, snapshot.id, identity,
                                             status="pending")

    # ---- the state machine -------------------------------------------------

    def _advance(self, occ_id: str, spool_path: str, name: str) -> None:
        try:
            while True:
                occurrence = records.get_occurrence(self.db, occ_id)
                state = occurrence["state"]
                if state == "recorded":
                    self._notify("upload", "Uploading encrypted archive")
                    self._publish(occurrence, spool_path)
                elif state == "transferred":
                    self._notify("payload-verify", "Verifying archived bytes")
                    self._verify(occurrence, spool_path)
                elif state == "verified":
                    self._notify("catalog-verify", "Verifying recoverable catalog")
                    self._prove_recoverable(occurrence)
                elif state == "recoverable":
                    self._notify("removal-check", "Checking safe local removal")
                    self._intend(occurrence, spool_path)
                elif state == "evicting":
                    self._notify("remove", "Removing local original")
                    self._delete(occurrence, spool_path)
                elif state == "evicted":
                    raise _Stop(Outcome.ARCHIVED)
                else:
                    raise _Stop(Outcome.REFUSED,
                                f"occurrence is {state}: {occurrence['last_error']}")
        except _Stop as stop:
            self._emit(stop.outcome, name, occ_id=occ_id, reason=stop.reason)
        except EngineError as error:
            reason = _engine_reason(error)
            self._fail_pending(occ_id, f"engine: {reason}")
            self._emit(Outcome.REFUSED, name, occ_id=occ_id, reason=reason)
        except OSError as error:
            self._fail_pending(occ_id, f"io: {error}")
            self._emit(Outcome.REFUSED, name, occ_id=occ_id,
                       reason=f"{error.strerror or error}")

    def _publish(self, occurrence, spool_path: str) -> None:
        """`recorded` → `transferred`: one attempt, one backup, gates (a), (b)."""
        occ_id = occurrence["occ_id"]
        pending = self._pending_attempt(occ_id)
        if pending is None:
            retry = attempts.retry_state(
                self.db, occ_id, max_attempts=self.config.max_attempts,
                retry_backoff_seconds=self.config.retry_backoff_seconds,
                now=time.time() + self.options.now_offset,
                retry_exhausted=self.options.retry_exhausted)
            if retry.decision == "deferred":
                raise _Stop(Outcome.DEFERRED, retry.reason)
            if retry.decision == "exhausted":
                raise _Stop(Outcome.REFUSED, retry.reason)
            if retry.decision == "confirmed":
                raise _Stop(Outcome.REFUSED,
                            "inconsistent store: a recorded occurrence holds a "
                            "confirmed attempt")

            started = attempts.start(self.db, occ_id, self.store_id,
                                     self.config.export_dir)
            fault_after("attempt-started")
            attempt_id = started.attempt_id
            identity = attempts.identity_for(self.db, attempt_id,
                                             occurrence["kind"])
            self._gate(occ_id, spool_path, attempt_id, snapshot_id=None,
                       confirmed=False)  # gate (a)
            try:
                result = self.engine.backup((spool_path, started.export_path),
                                            identity.to_tags())
            except EngineError as error:
                # Whether a snapshot exists is unknown; the attempt stays
                # pending and the next run asks the repository (resume).
                raise _Stop(Outcome.REFUSED,
                            f"backup failed; the attempt is left pending for the"
                            f" next run to reconcile: {_engine_reason(error)}")
            snapshot_id = result.snapshot_id
            # The crash the frontier gate was rewritten for: the
            # snapshot exists but its id was never recorded locally.
            fault_after("backup-returned")
            attempts.adopt(self.db, attempt_id, snapshot_id, identity)
            if result.exit_code != 0:
                reason = f"partial backup (restic exit {result.exit_code})"
                attempts.fail(self.db, attempt_id, reason, snapshot_id)
                raise _Stop(Outcome.REFUSED, reason)
        else:
            attempt_id = pending["attempt_id"]
            resumed = attempts.resume(self.db, self.engine, attempt_id)
            if resumed.snapshot_id is None:
                # Settled as failed (no snapshot, or duplicates); the retry
                # budget decides what happens next, on the next loop.
                return
            snapshot_id = resumed.snapshot_id

        self._gate(occ_id, spool_path, attempt_id, snapshot_id=snapshot_id,
                   confirmed=False)  # gate (b)
        with transaction(self.db):
            records.set_state(self.db, occ_id, "transferred")
        fault_after("transferred")

    def _verify(self, occurrence, spool_path: str) -> None:
        """`transferred` → `verified`: reconcile, read the bytes back, gate (c)."""
        occ_id = occurrence["occ_id"]
        pending = self._require_pending(occ_id)
        attempt_id, snapshot_id = pending["attempt_id"], pending["snapshot_id"]
        try:
            reconcile(self.engine, snapshot_id, occ_id, self.db, spool_path)
            verify_payload(self.engine, snapshot_id, occ_id, self.db, spool_path)
            if occurrence["kind"] != "file" and \
                    recompute_manifest_hash(self.db, occ_id) != occurrence["root_sha256"]:
                raise PayloadError("", "manifest hash differs from the record")
        except (ReconcileError, PayloadError) as error:
            reason = f"verification: {error}"
            attempts.fail(self.db, attempt_id, reason, snapshot_id)
            raise _Stop(Outcome.REFUSED, reason)
        self._gate(occ_id, spool_path, attempt_id, snapshot_id=snapshot_id,
                   confirmed=False)  # gate (c)
        with transaction(self.db):
            records.set_state(self.db, occ_id, "verified")
        fault_after("verified")

    def _prove_recoverable(self, occurrence) -> None:
        """`verified` → `recoverable`: the remote catalog proves itself."""
        occ_id = occurrence["occ_id"]
        pending = self._require_pending(occ_id)
        attempt_id, snapshot_id = pending["attempt_id"], pending["snapshot_id"]
        # The digest to prove against is the immutable snapshot tag, read back
        # from the repository, not the local row that produced it.
        published = [snapshot for snapshot in
                     self.engine.snapshots(tag=f"dropin:attempt={attempt_id}")
                     if snapshot.id == snapshot_id]
        if not published:
            reason = "catalog: snapshot is no longer in the repository"
            attempts.fail(self.db, attempt_id, reason, snapshot_id)
            raise _Stop(Outcome.REFUSED, reason)
        try:
            identity = parse_tags(published[0].tags)
            verify_catalog(engine=self.engine, snapshot_id=snapshot_id,
                           export_path=pending["export_path"], identity=identity,
                           connection=self.db, occ_id=occ_id,
                           tmp_dir=self.config.tmp_dir,
                           live_digest=pending["export_sha256"])
        except (CatalogError, TagError) as error:
            reason = (f"catalog: {error.check}" if isinstance(error, CatalogError)
                      else f"catalog: tags: {error}")
            attempts.fail(self.db, attempt_id, reason, snapshot_id)
            raise _Stop(Outcome.REFUSED, str(error))
        attempts.confirm(self.db, attempt_id, occ_id, snapshot_id,
                         pending["export_seq"])
        fault_after("recoverable")

    def _intend(self, occurrence, spool_path: str) -> None:
        """`recoverable` → `evicting`: gate (d), ownership, durable intent."""
        occ_id = occurrence["occ_id"]
        try:
            begin_eviction(self.db, occ_id, self.context.ownership, spool_path)
        except Retained as error:
            raise _Stop(Outcome.RETAINED, str(error))
        except (SourceChanged, SpecialEntry) as error:
            if not os.path.lexists(spool_path):
                # The user took it away between the scan and the intent. The
                # publication stands; the row goes dormant, like any other
                # no-spool occurrence, rather than being abandoned.
                raise _Stop(Outcome.REFUSED, "spool item vanished")
            attempts.abandon_source_changed(
                self.db, occ_id, occurrence["confirmed_attempt_id"],
                snapshot_id=None, confirmed=True)
            raise _Stop(Outcome.DEFERRED,
                        f"source changed after publication: {_detail(error)}")
        fault_after("intent-written")

    def _delete(self, occurrence, spool_path: str) -> None:
        """`evicting` → `evicted`: the journalled deletion, live or resumed."""
        try:
            run_eviction(self.db, occurrence["occ_id"], self.context.ownership,
                         spool_path)
        except Retained as error:
            raise _Stop(Outcome.RETAINED, str(error))

    # ---- gates (a)-(c) -----------------------------------------------------

    def _gate(self, occ_id: str, spool_path: str, attempt_id: str, *,
              snapshot_id: str | None, confirmed: bool) -> None:
        try:
            compare(self.db, occ_id, walk(spool_path))
        except (SourceChanged, SpecialEntry) as error:
            vanished = not os.path.lexists(spool_path)
            attempts.abandon_source_changed(self.db, occ_id, attempt_id,
                                            snapshot_id=snapshot_id,
                                            confirmed=confirmed)
            if vanished:
                raise _Stop(Outcome.REFUSED, "spool item vanished")
            raise _Stop(Outcome.DEFERRED, f"source changed: {_detail(error)}")

    # ---- helpers -----------------------------------------------------------

    def _pending_attempt(self, occ_id: str):
        return self.db.execute(
            "SELECT * FROM publication_attempt WHERE occ_id = ?"
            " AND outcome = 'pending' ORDER BY started_at DESC, export_seq DESC"
            " LIMIT 1", (occ_id,)).fetchone()

    def _require_pending(self, occ_id: str):
        """`transferred`/`verified` imply a live pending attempt with a snapshot."""
        pending = self._pending_attempt(occ_id)
        if pending is not None and pending["snapshot_id"]:
            return pending
        reason = "inconsistent store: no pending attempt with a snapshot"
        with transaction(self.db):
            if pending is not None:
                records.finish_attempt(self.db, pending["attempt_id"], "failed",
                                       reason=reason)
            records.set_state(self.db, occ_id, "recorded", error=reason)
        raise _Stop(Outcome.REFUSED, reason)

    def _fail_pending(self, occ_id: str, reason: str) -> None:
        pending = self._pending_attempt(occ_id)
        if pending is not None:
            attempts.fail(self.db, pending["attempt_id"], reason)

    def _emit(self, outcome: Outcome, name: str, *, occ_id: str | None = None,
              reason: str | None = None) -> None:
        fields: dict = {}
        if occ_id is not None:
            occurrence = records.get_occurrence(self.db, occ_id)
            snapshot = self.db.execute(
                "SELECT snapshot_id FROM publication_attempt WHERE occ_id = ?"
                " AND outcome IN ('confirmed', 'pending')"
                " ORDER BY outcome = 'confirmed' DESC, started_at DESC LIMIT 1",
                (occ_id,)).fetchone()
            fields = {
                "archive_path": occurrence["archive_path"],
                "kind": occurrence["kind"],
                "state": occurrence["state"],
                "snapshot": snapshot["snapshot_id"] if snapshot else None,
                "sha256": occurrence["root_sha256"],
                "size": occurrence["size_bytes"],
                "dedup": occurrence["dedup_of"] is not None,
            }
        self.report.item(outcome, name, reason=reason, **fields)
        with transaction(self.db):
            seq = self.db.execute(
                "SELECT coalesce(max(seq), 0) + 1 FROM run_event WHERE run_id = ?",
                (self.report.run_id,)).fetchone()[0]
            self.db.execute(
                "INSERT INTO run_event (run_id, seq, occ_id, outcome, name,"
                " reason, recorded_at) VALUES (?,?,?,?,?,?,?)",
                (self.report.run_id, seq, occ_id, outcome.value, name, reason,
                 _now_text()))


def _engine_reason(error: EngineError) -> str:
    """Keep backend/lock-holder diagnostics without dumping configuration.

    The real adapter already limits stderr to 20 lines; also cap characters
    here so an injected or unusually long backend line cannot swamp reports.
    """
    tail = "\n".join(error.stderr_tail.splitlines()[-20:])[-8192:]
    return f"{error}\n{tail}" if tail else str(error)


def _detail(error: Exception) -> str:
    if isinstance(error, SourceChanged):
        where = error.rel_path or "."
        return f"{error.detail} at {where}" if error.detail else where
    return str(error)


def _now_text() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _tree_size(root: Path) -> int:
    total = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for filename in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, filename)).st_size
            except OSError:
                continue
    return total


def _empty_directory(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for entry in os.scandir(root):
        if entry.is_dir(follow_symlinks=False):
            shutil.rmtree(entry.path, ignore_errors=True)
        else:
            try:
                os.unlink(entry.path)
            except FileNotFoundError:
                pass
