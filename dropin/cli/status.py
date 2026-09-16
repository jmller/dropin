"""`dropin status`: one strictly observational operational health snapshot."""
from __future__ import annotations

import json
import os
import sys

from ..engine.interface import EngineError
from ..engine.tools import ToolGateError, check_tools
from ..pipeline.frontier import (LineageMismatch, StaleStore, assess_frontier)
from ..pipeline.writer_lock import current_holder
from ..query.filters import STATES
from ..query.search import read_store
from ..report import status_exit_code
from ..spool.scan import scan
from ..store import records

ACTIVE_DORMANT_STATES = ("recorded", "transferred", "verified", "recoverable")


def run(context, args) -> int:
    attention: list[str] = []
    tools, tools_ok = _tools(context)
    holder = current_holder(context.config.writer_lock_path)
    if holder is not None:
        attention.append("writer lock is held")

    capabilities = context.ownership.capabilities()
    validation = getattr(context.macos, "validation_state", "unvalidated")
    adapter = {
        "macos": validation,
        "ownership": "supported" if capabilities.ownership_check else "unsupported",
        "reason": capabilities.ownership_reason or None,
        "scope": capabilities.scope_note,
    }
    if validation == "unvalidated":
        attention.append("macOS adapter is unvalidated")
    if not capabilities.ownership_check:
        attention.append("ownership check is unsupported")

    with read_store(context.config) as db:
        local = _local_status(db, context.config, attention)
        store_id = records.store_meta(db)["store_id"]
        repository = {"status": "not-checked", "reason": None}
        frontier = {"status": "not-checked", "reason": None}
        unreachable = False
        if not args.offline and tools_ok:
            try:
                assess_frontier(db, context.engine, store_id)
            except StaleStore as error:
                repository = {"status": "reachable", "reason": None}
                frontier = {"status": "stale", "reason": str(error)}
                attention.append("repository frontier is stale")
            except LineageMismatch as error:
                repository = {"status": "reachable", "reason": None}
                frontier = {"status": "lineage-mismatch", "reason": str(error)}
                attention.append("repository lineage mismatch")
            except EngineError as error:
                repository = {"status": "unreachable", "reason": str(error)}
                frontier = {"status": "not-checked", "reason": "repository unavailable"}
                unreachable = True
            else:
                repository = {"status": "reachable", "reason": None}
                frontier = {"status": "ok", "reason": None}
        elif not args.offline:
            repository = {"status": "unreachable", "reason": tools["reason"]}
            frontier = {"status": "not-checked", "reason": "tool gate failed"}
            unreachable = True
        elif not tools_ok:
            attention.append("tool gate failed")

    payload = {
        "tools": tools,
        "adapter": adapter,
        "writer_lock": holder,
        "repository": repository,
        "frontier": frontier,
        **local,
        "attention": attention,
    }
    _render(context, payload)
    return status_exit_code(attention=bool(attention), unreachable=unreachable,
                            offline=args.offline)


def _tools(context):
    if context.fake_engine:
        return ({"status": "ok", "reason": None,
                 "restic": {"version": context.config.restic_min,
                            "path": context.config.restic},
                 "rclone": {"version": context.config.rclone_min,
                            "path": context.config.rclone}}, True)
    try:
        versions = check_tools(context.config)
    except ToolGateError as error:
        return ({"status": "error", "reason": str(error),
                 "restic": {"version": None, "path": context.config.restic},
                 "rclone": {"version": None, "path": context.config.rclone}}, False)
    return ({"status": "ok", "reason": None,
             "restic": {"version": _version(versions.restic),
                        "path": versions.restic_path},
             "rclone": {"version": _version(versions.rclone),
                        "path": versions.rclone_path}}, True)


def _version(value) -> str:
    return ".".join(map(str, value))


def _local_status(db, config, attention: list[str]) -> dict:
    state_counts = {state: 0 for state in STATES}
    for row in db.execute("SELECT state,count(*) AS n FROM occurrence GROUP BY state"):
        state_counts[row["state"]] = row["n"]

    active = list(db.execute(
        "SELECT occ_id,spool_path FROM occurrence WHERE state IN (?,?,?,?)",
        ACTIVE_DORMANT_STATES))
    dormant_ids = {row["occ_id"] for row in active
                   if not os.path.lexists(row["spool_path"])}
    unresolved = 0
    if dormant_ids:
        placeholders = ",".join("?" for _ in dormant_ids)
        unresolved = db.execute(
            f"SELECT count(DISTINCT a.attempt_id) FROM publication_attempt a"
            f" WHERE a.outcome='pending' AND a.occ_id IN ({placeholders})"
            " AND EXISTS(SELECT 1 FROM snapshot s WHERE s.attempt_id=a.attempt_id)",
            tuple(dormant_ids)).fetchone()[0]

    exhausted = db.execute(
        "SELECT count(*) FROM occurrence o WHERE o.confirmed_attempt_id IS NULL"
        " AND NOT EXISTS(SELECT 1 FROM publication_attempt p"
        " WHERE p.occ_id=o.occ_id AND p.outcome='pending')"
        " AND (SELECT count(*) FROM publication_attempt f"
        " WHERE f.occ_id=o.occ_id AND f.outcome='failed') >= ?",
        (config.max_attempts,)).fetchone()[0]
    orphaned = db.execute(
        "SELECT count(*) FROM snapshot WHERE status='orphaned'").fetchone()[0]

    latest = db.execute(
        "SELECT run_id FROM run WHERE verb='drain' AND finished_at IS NOT NULL"
        " ORDER BY finished_at DESC,run_id DESC LIMIT 1").fetchone()
    event_counts = {"deferred": 0, "retained": 0, "refused": 0}
    if latest is not None:
        for row in db.execute(
                "SELECT outcome,count(*) AS n FROM run_event WHERE run_id=?"
                " AND outcome IN ('deferred','retained','refused') GROUP BY outcome",
                (latest["run_id"],)):
            event_counts[row["outcome"]] = row["n"]

    try:
        spool_items = sum(1 for _ in scan(config.drop_dir))
    except OSError as error:
        spool_items = 0
        attention.append(f"spool scan failed: {error}")

    cache_bytes, cache_errors = _tree_size(config.cache_dir)
    tmp_bytes, tmp_errors = _tree_size(config.tmp_dir)
    measurement_errors = [*cache_errors, *tmp_errors]
    cache_ceiling = config.cache_max_mb * 1024 * 1024
    tmp_budget = (config.pack_size_mb * (config.rclone_connections + 1)
                  * 1024 * 1024)
    if measurement_errors:
        attention.append("resource measurement incomplete")
    if cache_bytes > cache_ceiling:
        attention.append("cache exceeds its ceiling")
    if tmp_bytes > tmp_budget:
        attention.append("tmp exceeds its transport budget")
    if event_counts["retained"]:
        attention.append("latest drain retained items")
    if event_counts["refused"]:
        attention.append("latest drain refused items")
    if exhausted:
        attention.append("publication attempts are exhausted")
    if unresolved:
        attention.append("dormant publication attempts are unresolved")
    if orphaned:
        attention.append("orphaned snapshots exist")

    return {
        "spool": {"items": spool_items,
                  "deferred": event_counts["deferred"],
                  "retained": event_counts["retained"],
                  "attempts_exhausted": exhausted,
                  "refused_last_run": event_counts["refused"]},
        "records": {"states": state_counts, "dormant": len(dormant_ids),
                    "orphaned_snapshots": orphaned,
                    "unresolved_attempts": unresolved},
        "resources": {"cache_bytes": cache_bytes,
                      "cache_ceiling_bytes": cache_ceiling,
                      "tmp_bytes": tmp_bytes, "tmp_budget_bytes": tmp_budget,
                      "measurement_errors": measurement_errors,
                      "last_restic_peak_rss_kb": _scalar(db,
                          "SELECT restic_peak_rss_kb FROM run WHERE verb='drain'"
                          " AND finished_at IS NOT NULL AND restic_peak_rss_kb IS NOT NULL"
                          " ORDER BY finished_at DESC,run_id DESC LIMIT 1")},
        "last_success": {"drain": _last_success(db, "drain"),
                         "verify": _last_success(db, "verify"),
                         "export": _scalar(db,
                             "SELECT max(finished_at) FROM publication_attempt"
                             " WHERE outcome='confirmed'")},
    }


def _last_success(db, verb: str):
    return _scalar(db, "SELECT max(finished_at) FROM run WHERE verb=? AND exit_code=0",
                   (verb,))


def _scalar(db, sql: str, values=()):
    row = db.execute(sql, values).fetchone()
    return row[0] if row is not None else None


def _tree_size(root) -> tuple[int, list[str]]:
    total = 0
    errors: list[str] = []

    def onerror(error: OSError) -> None:
        errors.append(f"{getattr(error, 'filename', None) or root}: {error}")

    try:
        entries = os.walk(root, onerror=onerror)
        for dirpath, _dirnames, filenames in entries:
            for name in filenames:
                path = os.path.join(dirpath, name)
                try:
                    total += os.lstat(path).st_size
                except OSError as error:
                    errors.append(f"{path}: {error}")
    except OSError as error:
        onerror(error)
    return total, errors


def _render(context, payload) -> None:
    if context.json_output:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        for key, value in payload.items():
            print(f"{key}\t{json.dumps(value, ensure_ascii=False, sort_keys=True)}")
    sys.stdout.flush()
