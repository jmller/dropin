"""`dropin verify`: data-read audit of confirmed archive occurrences."""
from __future__ import annotations

from datetime import datetime, timezone
import os
import re
import sys

from ..engine.interface import EngineError
from ..pipeline.verify import PayloadError, verify_payload
from ..query.filters import QueryError, configure, date_bound
from ..report import Outcome, Report
from ..store import records
from ..store.db import transaction
from . import emit, tools_gate

SUBSET_RE = re.compile(r"[1-9][0-9]*/[1-9][0-9]*\Z")


def run(context, args) -> int:
    report = Report("verify", records.new_run_id())
    problem = _validate(args)
    if problem:
        print(f"dropin: verify: {problem}", file=sys.stderr)
        return 2

    problem = tools_gate(context)
    if problem:
        report.run_refusal = problem
        return emit(context, report)

    db = context.db
    _open_run(db, report.run_id)
    try:
        try:
            # Reachability is established before item outcomes are emitted.
            context.engine.snapshots(tag="dropin:v=1")
        except EngineError as error:
            report.run_refusal = str(error)
        else:
            selected = _select(db, args.paths, args.since)
            seen: set[str] = set()
            for requested, occurrence, snapshot_id in selected:
                if occurrence is None:
                    report.item(Outcome.MISSING, requested, archive_path=requested,
                                reason="unknown or unconfirmed archive path")
                    continue
                occ_id = occurrence["occ_id"]
                if occ_id in seen:
                    continue
                seen.add(occ_id)
                fields = _fields(occurrence, snapshot_id)
                if not snapshot_id:
                    report.item(Outcome.MISSING, occurrence["item_name"],
                                reason="confirmed attempt has no snapshot", **fields)
                    continue
                try:
                    verify_payload(context.engine, snapshot_id, occ_id, db,
                                   occurrence["spool_path"])
                except (PayloadError, EngineError) as error:
                    outcome = (Outcome.MISSING if _is_missing(error)
                               else Outcome.CORRUPT)
                    report.item(outcome, occurrence["item_name"], reason=str(error),
                                **fields)
                else:
                    report.item(Outcome.VERIFIED, occurrence["item_name"], **fields)

            if args.repo:
                subset = args.subset or "1/1"
                try:
                    context.engine.check(subset)
                except EngineError as error:
                    outcome = (Outcome.MISSING if error.kind == "missing"
                               else Outcome.CORRUPT)
                    report.item(outcome, "repository", reason=str(error))
                else:
                    report.item(Outcome.VERIFIED, "repository",
                                reason=f"read-data-subset {subset}")
    finally:
        _close_run(db, report)
    return emit(context, report)


def _validate(args) -> str | None:
    if args.all and args.paths:
        return "--all and explicit paths are mutually exclusive"
    if args.subset and not args.repo:
        return "--subset requires --repo"
    if args.since is not None:
        try:
            date_bound(args.since)
        except QueryError as error:
            return str(error)
    if args.subset:
        if not SUBSET_RE.fullmatch(args.subset):
            return "--subset must be canonical N/M with 1 <= N <= M"
        left, right = args.subset.split("/", 1)
        try:
            invalid = int(left) > int(right)
        except ValueError:
            invalid = True
        if invalid:
            return "--subset must be canonical N/M with 1 <= N <= M"
    return None


def _select(db, paths: list[str], since: str | None):
    configure(db)
    since_value = date_bound(since)[0] if since is not None else None
    join = (" FROM occurrence o LEFT JOIN publication_attempt a"
            " ON a.attempt_id=o.confirmed_attempt_id")
    if not paths:
        sql = ("SELECT o.*,a.snapshot_id" + join
               + " WHERE o.confirmed_attempt_id IS NOT NULL")
        values: list[str] = []
        if since_value is not None:
            sql += " AND query_date(o.recorded_at) >= ?"
            values.append(since_value)
        sql += " ORDER BY o.recorded_at,o.occ_id"
        return [(row["archive_path"], row, row["snapshot_id"])
                for row in db.execute(sql, values)]

    selected = []
    for path in paths:
        row = db.execute(
            "SELECT o.*,a.snapshot_id" + join
            + " JOIN entry e ON e.occ_id=o.occ_id"
            " WHERE e.archive_path=? AND o.confirmed_attempt_id IS NOT NULL",
            (path,)).fetchone()
        if row is not None and since_value is not None:
            stored = db.execute("SELECT query_date(?)", (row["recorded_at"],)).fetchone()[0]
            if stored is None or stored < since_value:
                continue
        selected.append((path, row, row["snapshot_id"] if row else None))
    return selected


def _fields(occurrence, snapshot_id: str | None) -> dict:
    return {"archive_path": occurrence["archive_path"],
            "kind": occurrence["kind"], "state": occurrence["state"],
            "snapshot": snapshot_id, "sha256": occurrence["root_sha256"],
            "size": occurrence["size_bytes"],
            "dedup": occurrence["dedup_of"] is not None}


def _is_missing(error: BaseException) -> bool:
    current: BaseException | None = error
    while current is not None:
        if isinstance(current, EngineError) and current.kind == "missing":
            return True
        current = current.__cause__
    return False


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _open_run(db, run_id: str) -> None:
    with transaction(db):
        db.execute("INSERT INTO run(run_id,verb,started_at,writer_lock_pid)"
                   " VALUES(?, 'verify', ?, ?)", (run_id, _now(), os.getpid()))


def _close_run(db, report: Report) -> None:
    with transaction(db):
        for seq, record in enumerate(report.records, 1):
            db.execute("INSERT INTO run_event(run_id,seq,outcome,name,reason,recorded_at)"
                       " VALUES(?,?,?,?,?,?)",
                       (report.run_id, seq, record.outcome.value, record.name,
                        record.reason, _now()))
        db.execute("UPDATE run SET finished_at=?,exit_code=? WHERE run_id=?",
                   (_now(), report.exit_code(), report.run_id))
