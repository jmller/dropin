"""Fresh recovery: a store from the repository and the password alone.

The recovery protocol, step for step:

1. observe every `dropin:v=1` snapshot and strictly parse its seven tags;
2. dump each snapshot's catalog export into an immutable input file and prove
   it (digest tag, integrity, lineage row) *before* opening it for anything;
3. choose the base: the valid export with the highest sequence of the newest
   lineage; byte-copy it to the one writable file;
4. merge every other valid export into that copy, failing closed on an
   identifier that names two different things;
5. verify every occurrence independently against its own snapshots, newest
   first, deriving confirmation from evidence rather than from any record;
6. rebuild the watermarks and lineage from what was observed;
7. publish atomically, so an interrupted recovery leaves only `recover.tmp`.

Nothing here ever claims a local deletion happened: a prior `evicted` becomes
`recoverable`, and a prior `evicting` keeps its intent, marked as reconstructed,
so the next drain may resume it only if the root exists and fully verifies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import sqlite3

from .engine.interface import EngineError, Identity, TagError, parse_tags
from .pipeline.catalog_verify import CatalogError, verify_catalog_file
from .pipeline.reconcile import ReconcileError, reconcile
from .pipeline.states import ORDER, State
from .pipeline.verify import PayloadError, verify_payload
from .pipeline.writer_lock import writer_lock
from .report import Outcome, Report
from .store import records
from .store.db import connect, transaction

NO_PROOF = "recovered without completion proof"
MANUAL = ("retained: manual intervention (recovered evicting intent; resumes "
          "only if the root exists, fully verifies, and ownership is clear)")

#: Tables merged per occurrence, in dependency order.
OCCURRENCE_TABLES = ("entry", "fingerprint", "attribute", "xattr", "normalized",
                     "tag")
#: Occurrence columns that may never differ between two exports of one id.
OCCURRENCE_IDENTITY = ("origin_store_id", "item_name", "archive_path", "kind",
                       "spool_path", "root_sha256", "size_bytes", "entry_count")
ATTEMPT_IDENTITY = ("origin_store_id", "occ_id", "export_seq", "started_at")


class RecoverRefused(Exception):
    """Nothing was written. The message says why."""


@dataclass
class Observation:
    snapshot_id: str
    time: str
    paths: tuple[str, ...]
    tags: tuple[str, ...]
    identity: Identity
    export_path: str | None = None
    dumped: Path | None = None
    valid: bool = False
    reason: str | None = None
    #: Final ledger status, decided in step 5.
    status: str = "pending"


@dataclass
class RecoverResult:
    store_path: Path
    store_id: str
    counts: dict[str, int] = field(default_factory=dict)
    observations: list[Observation] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)


def recover(engine, state_dir: Path | str, report: Report, *,
            trust_later_exports: bool = False) -> RecoverResult:
    state_dir = Path(state_dir)
    store_path = state_dir / "store.sqlite"
    if store_path.exists():
        raise RecoverRefused(
            f"{store_path} already exists; recovery never overwrites a store. "
            f"Point --into at a new empty directory")
    state_dir.mkdir(parents=True, exist_ok=True)

    with writer_lock(state_dir / "writer.lock", verb="recover"):
        work_dir = state_dir / "recover.tmp"
        if work_dir.exists():
            shutil.rmtree(work_dir)  # an interrupted run; rebuilt from scratch
        exports_dir = work_dir / "exports"
        exports_dir.mkdir(parents=True)

        observations, skipped = _observe(engine, report)
        if not observations and not skipped:
            raise RecoverRefused("the repository holds no archiver snapshots")
        _dump_candidates(engine, observations, exports_dir)

        base = _choose_base(observations)
        work_store = work_dir / "store.sqlite"
        if base is not None:
            _byte_copy(base.dumped, work_store)
            store_id = base.identity.store_id
        else:
            # Parseable snapshots but no usable catalog: the store can hold the
            # observation ledger and nothing else.
            store_id = _newest(observations).identity.store_id
            with _closing(connect(work_store)) as fresh:
                records.initialise_store(fresh, store_id)

        db = connect(work_store)
        try:
            _merge(db, observations, base)
            _ledger(db, observations)
            _verify_occurrences(db, engine, observations, report,
                                trust_later_exports)
            _rebuild_meta(db, observations, store_id, base)
            counts = _counts(db)
            _publish(db, work_store, store_path, state_dir)
        finally:
            try:
                db.close()
            except sqlite3.ProgrammingError:
                pass
        shutil.rmtree(work_dir, ignore_errors=True)
        for name in ("export", "cache", "tmp"):
            (state_dir / name).mkdir(exist_ok=True)

    _report_summary(report, counts, observations, skipped)
    return RecoverResult(store_path, store_id, counts, observations, skipped)


# ---- step 1: observe -------------------------------------------------------

def _observe(engine, report: Report):
    observations: list[Observation] = []
    skipped: list[tuple[str, str]] = []
    for snapshot in engine.snapshots(tag="dropin:v=1"):
        try:
            identity = parse_tags(snapshot.tags)
        except TagError as error:
            reason = f"skipped: malformed reserved tags ({error})"
            skipped.append((snapshot.id, reason))
            report.item(Outcome.ORPHANED, snapshot.id, reason=reason)
            continue
        observations.append(Observation(snapshot.id, snapshot.time,
                                        tuple(snapshot.paths), tuple(snapshot.tags),
                                        identity))
    return observations, skipped


# ---- step 2: candidate exports ---------------------------------------------

def _dump_candidates(engine, observations, exports_dir: Path) -> None:
    for observation in observations:
        identity = observation.identity
        suffix = f"{identity.occ_id}-{identity.attempt_id}.sqlite"
        matches = [path for path in observation.paths if path.endswith(suffix)]
        if len(matches) != 1:
            observation.status = "orphaned"
            observation.reason = ("catalog: export path missing" if not matches
                                  else "catalog: ambiguous export path")
            continue
        observation.export_path = matches[0]
        target = exports_dir / f"{observation.snapshot_id}.sqlite"
        try:
            with engine.dump(observation.snapshot_id, matches[0]) as stream:
                with open(target, "wb") as handle:
                    shutil.copyfileobj(stream, handle, 1024 * 1024)
            # Immutable input from here on: SQLite opens a read-only file
            # read-only, so no later step can write to it by accident.
            os.chmod(target, 0o444)
            verify_catalog_file(target, identity)
        except (EngineError, OSError, CatalogError) as error:
            check = error.check if isinstance(error, CatalogError) else "dump"
            observation.status = "orphaned"
            observation.reason = f"catalog: {check}: {error}"
            continue
        observation.dumped = target
        observation.valid = True


# ---- step 3: the base ------------------------------------------------------

def _choose_base(observations) -> Observation | None:
    valid = [o for o in observations if o.valid]
    if not valid:
        return None
    # The newest lineage by time picks the store; within it, the highest
    # sequence is the most complete catalog.
    newest_store = _newest(valid).identity.store_id
    own = [o for o in valid if o.identity.store_id == newest_store]
    return max(own, key=lambda o: o.identity.export_seq)


def _newest(observations) -> Observation:
    return max(observations, key=lambda o: (o.time, o.snapshot_id))


def _byte_copy(source: Path, target: Path) -> None:
    shutil.copyfile(source, target)
    os.chmod(target, 0o600)
    fd = os.open(target, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


# ---- step 4: merge ---------------------------------------------------------

def _merge(db, observations, base: Observation | None) -> None:
    others = sorted((o for o in observations if o.valid and o is not base),
                    key=lambda o: (o.identity.store_id, o.identity.export_seq))
    for observation in others:
        _merge_export(db, observation)
    # Every reconstructed intent is marked: an absent root is never proof.
    with transaction(db):
        db.execute("UPDATE eviction_intent SET recovered_without_local_history = 1")


def observation_store(db) -> str:
    return db.execute("SELECT store_id FROM src.export_lineage").fetchone()[0]


def observation_seq(db) -> int:
    return db.execute("SELECT export_seq FROM src.export_lineage").fetchone()[0]


def _merge_export(db, observation: Observation) -> None:
    db.execute("ATTACH DATABASE ? AS src", (str(observation.dumped),))
    try:
        with transaction(db):
            for occurrence in db.execute("SELECT * FROM src.occurrence").fetchall():
                _merge_occurrence(db, occurrence)
            for attempt in db.execute("SELECT * FROM src.publication_attempt"):
                _merge_attempt(db, attempt, observation)
            for row in db.execute("SELECT * FROM src.export_lineage"):
                db.execute(
                    "INSERT OR IGNORE INTO export_lineage (store_id, export_seq,"
                    " occ_id, attempt_id, exported_at) VALUES (?,?,?,?,?)",
                    (row["store_id"], row["export_seq"], row["occ_id"],
                     row["attempt_id"], row["exported_at"]))
            for row in db.execute("SELECT * FROM src.eviction_intent"):
                db.execute(
                    "INSERT OR IGNORE INTO eviction_intent (occ_id, intent_at,"
                    " fingerprint_json, progress_json,"
                    " recovered_without_local_history) VALUES (?,?,?,?,1)",
                    (row["occ_id"], row["intent_at"], row["fingerprint_json"],
                     row["progress_json"]))
            for row in db.execute("SELECT * FROM src.lineage"):
                records.merge_lineage(db, row["store_id"], row["merged_through_seq"])
    finally:
        db.execute("DETACH DATABASE src")


def _merge_occurrence(db, incoming) -> None:
    occ_id = incoming["occ_id"]
    existing = db.execute("SELECT * FROM main.occurrence WHERE occ_id = ?",
                          (occ_id,)).fetchone()
    if existing is None:
        columns = _columns(db, "occurrence")
        db.execute(
            f"INSERT INTO main.occurrence ({', '.join(columns)})"
            f" SELECT {', '.join(columns)} FROM src.occurrence WHERE occ_id = ?",
            (occ_id,))
        for table in OCCURRENCE_TABLES:
            columns = _columns(db, table)
            db.execute(
                f"INSERT INTO main.{table} ({', '.join(columns)})"
                f" SELECT {', '.join(columns)} FROM src.{table} WHERE occ_id = ?",
                (occ_id,))
        return

    for column in OCCURRENCE_IDENTITY:
        if existing[column] != incoming[column]:
            raise records.IdentityCollision(
                f"identifier collision: occurrence {occ_id} has {column}="
                f"{existing[column]!r} in one export and {incoming[column]!r} "
                f"in another")
    for table in ("entry", "fingerprint"):
        _require_equal_rows(db, table, occ_id)
    # Same identity, same immutable children: a later export may know a higher
    # state. Take it, so verification starts from the most complete record;
    # step 5 re-derives what is actually proven. `abandoned` is terminal: it is
    # never replaced, and it replaces a pipeline state only when it comes from
    # a later export of the occurrence's own lineage.
    if existing["state"] == "abandoned":
        return
    if incoming["state"] == "abandoned":
        take = (observation_store(db) == existing["origin_store_id"]
                and observation_seq(db) > _base_seq_for(db, existing["origin_store_id"]))
    else:
        take = _rank(incoming["state"]) > _rank(existing["state"])
    if take:
        columns = [c for c in _columns(db, "occurrence") if c != "occ_id"]
        assignments = ", ".join(f"{c} = (SELECT {c} FROM src.occurrence"
                                f" WHERE occ_id = ?)" for c in columns)
        db.execute(f"UPDATE main.occurrence SET {assignments} WHERE occ_id = ?",
                   (*([occ_id] * len(columns)), occ_id))


def _require_equal_rows(db, table: str, occ_id: str) -> None:
    columns = ", ".join(_columns(db, table))
    difference = db.execute(
        f"SELECT count(*) FROM ("
        f" SELECT {columns} FROM main.{table} WHERE occ_id = ?"
        f" EXCEPT SELECT {columns} FROM src.{table} WHERE occ_id = ?)"
        f" UNION ALL SELECT count(*) FROM ("
        f" SELECT {columns} FROM src.{table} WHERE occ_id = ?"
        f" EXCEPT SELECT {columns} FROM main.{table} WHERE occ_id = ?)",
        (occ_id, occ_id, occ_id, occ_id)).fetchall()
    if any(row[0] for row in difference):
        raise records.IdentityCollision(
            f"identifier collision: occurrence {occ_id} has different {table} "
            f"rows in two exports")


def _merge_attempt(db, incoming, observation: Observation) -> None:
    attempt_id = incoming["attempt_id"]
    existing = db.execute(
        "SELECT * FROM main.publication_attempt WHERE attempt_id = ?",
        (attempt_id,)).fetchone()
    if existing is None:
        columns = _columns(db, "publication_attempt")
        db.execute(
            f"INSERT INTO main.publication_attempt ({', '.join(columns)})"
            f" SELECT {', '.join(columns)} FROM src.publication_attempt"
            f" WHERE attempt_id = ?", (attempt_id,))
        return
    for column in ATTEMPT_IDENTITY:
        if existing[column] != incoming[column]:
            raise records.IdentityCollision(
                f"identifier collision: attempt {attempt_id} has {column}="
                f"{existing[column]!r} in one export and {incoming[column]!r} "
                f"in another")
    # An outcome recorded by a later export of the same lineage is the more
    # authenticated history; a pending row never outranks it.
    base_seq = _base_seq_for(db, existing["origin_store_id"])
    if (existing["outcome"] == "pending" and incoming["outcome"] != "pending"
            and observation.identity.store_id == existing["origin_store_id"]
            and observation.identity.export_seq > base_seq):
        db.execute(
            "UPDATE main.publication_attempt SET outcome = ?, reason = ?,"
            " finished_at = ?, snapshot_id = coalesce(snapshot_id, ?)"
            " WHERE attempt_id = ?",
            (incoming["outcome"], incoming["reason"], incoming["finished_at"],
             incoming["snapshot_id"], attempt_id))


def _base_seq_for(db, store_id: str) -> int:
    row = db.execute("SELECT export_seq FROM main.export_lineage"
                     " WHERE store_id = ? ORDER BY export_seq DESC LIMIT 1",
                     (store_id,)).fetchone()
    return row[0] if row else 0


def _columns(db, table: str) -> list[str]:
    return [row[1] for row in db.execute(f"PRAGMA main.table_info({table})")]


def _rank(state: str) -> int:
    try:
        return ORDER.index(State(state))
    except ValueError:
        return -1  # abandoned: terminal, never "higher"


# ---- step 4b: the observation ledger ---------------------------------------

def _ledger(db, observations) -> None:
    """Every strictly parseable snapshot, exact tags, before any verdict."""
    with transaction(db):
        for observation in observations:
            existing = records.get_snapshot(db, observation.snapshot_id)
            if observation.valid:
                status = existing["status"] if existing is not None else "pending"
                reason = existing["reason"] if existing is not None else None
            else:
                status, reason = "orphaned", observation.reason
            records.observe_snapshot(db, observation.snapshot_id,
                                     observation.identity, status=status,
                                     reason=reason)


# ---- step 5: verify every occurrence ---------------------------------------

def _verify_occurrences(db, engine, observations, report: Report,
                        trust_later_exports: bool) -> None:
    by_occurrence: dict[str, list[Observation]] = {}
    for observation in observations:
        by_occurrence.setdefault(observation.identity.occ_id, []).append(observation)

    known: set[str] = set()
    for occurrence in db.execute("SELECT * FROM occurrence ORDER BY recorded_at,"
                                 " occ_id").fetchall():
        known.add(occurrence["occ_id"])
        if occurrence["state"] == "abandoned":
            continue  # terminal; its evidence is left exactly as recorded
        candidates = by_occurrence.get(occurrence["occ_id"], [])
        confirmed = _confirm_one(db, engine, occurrence, candidates,
                                 trust_later_exports)
        _settle_occurrence(db, occurrence, candidates, confirmed, report)

    # A snapshot whose occurrence no valid catalog describes cannot be
    # recovered: its manifest is unknown. Loud, and never silently dropped.
    for occ_id, candidates in by_occurrence.items():
        if occ_id in known:
            continue
        for observation in candidates:
            report.item(Outcome.REFUSED, observation.snapshot_id,
                        reason=f"unrecoverable: no valid catalog export describes "
                               f"occurrence {occ_id} ({observation.reason})")


def _confirm_one(db, engine, occurrence, candidates, trust: bool):
    """The first snapshot, newest first, that proves itself; None otherwise."""
    eligible = []
    for observation in candidates:
        attempt = db.execute(
            "SELECT * FROM publication_attempt WHERE attempt_id = ?",
            (observation.identity.attempt_id,)).fetchone()
        if attempt is not None and attempt["outcome"] == "failed":
            # Authenticated history excludes it, whatever its payload says.
            observation.status = "orphaned"
            observation.reason = (f"excluded: a later export records the attempt "
                                  f"failed ({attempt['reason']})")
        elif observation.valid:
            eligible.append(observation)
    for observation in sorted(eligible, key=lambda o: (o.time, o.snapshot_id),
                              reverse=True):
        if trust and _later_export_confirms(occurrence, observation):
            return observation
        try:
            reconcile(engine, observation.snapshot_id, occurrence["occ_id"], db,
                      occurrence["spool_path"])
            verify_payload(engine, observation.snapshot_id, occurrence["occ_id"],
                           db, occurrence["spool_path"])
        except (ReconcileError, PayloadError, EngineError) as error:
            observation.status = "orphaned"
            observation.reason = f"verification: {error}"
            continue
        return observation
    return None


def _later_export_confirms(occurrence, observation: Observation) -> bool:
    return (occurrence["confirmed_attempt_id"] == observation.identity.attempt_id
            and occurrence["state"] in ("recoverable", "evicting", "evicted"))


def _settle_occurrence(db, occurrence, candidates, confirmed, report) -> None:
    occ_id = occurrence["occ_id"]
    prior = occurrence["state"]
    with transaction(db):
        if confirmed is not None:
            identity = confirmed.identity
            db.execute(
                "UPDATE publication_attempt SET outcome = 'confirmed', reason = NULL,"
                " finished_at = coalesce(finished_at, ?), snapshot_id = ?,"
                " export_sha256 = ? WHERE attempt_id = ?",
                (_now(), confirmed.snapshot_id, identity.catalog_sha256,
                 identity.attempt_id))
            if prior == "evicting":
                db.execute("UPDATE occurrence SET confirmed_attempt_id = ?,"
                           " last_error = ? WHERE occ_id = ?",
                           (identity.attempt_id, MANUAL, occ_id))
            else:
                # Repository evidence cannot prove a local deletion happened.
                db.execute(
                    "UPDATE occurrence SET state = 'recoverable',"
                    " confirmed_attempt_id = ?, recoverable_at = coalesce("
                    "recoverable_at, ?), evicting_at = NULL, evicted_at = NULL,"
                    " last_error = NULL WHERE occ_id = ?",
                    (identity.attempt_id, _now(), occ_id))
            for other in candidates:
                if other is confirmed:
                    other.status, other.reason = "confirmed", None
                elif other.status != "orphaned":
                    other.status = "orphaned"
                    other.reason = f"superseded by confirmed {confirmed.snapshot_id}"
        else:
            for other in candidates:
                if other.status != "orphaned":
                    other.status, other.reason = "orphaned", NO_PROOF
            if prior == "evicting":
                db.execute("UPDATE occurrence SET last_error = ? WHERE occ_id = ?",
                           (MANUAL, occ_id))
            else:
                db.execute(
                    "UPDATE occurrence SET state = 'recorded',"
                    " confirmed_attempt_id = NULL, last_error = ? WHERE occ_id = ?",
                    (NO_PROOF, occ_id))
                db.execute(
                    "UPDATE publication_attempt SET outcome = 'failed', reason = ?,"
                    " finished_at = coalesce(finished_at, ?) WHERE occ_id = ?"
                    " AND outcome != 'failed'", (NO_PROOF, _now(), occ_id))
        for observation in candidates:
            records.set_snapshot_status(db, observation.snapshot_id,
                                        observation.status, observation.reason)

    name = occurrence["item_name"]
    if prior == "evicting":
        report.item(Outcome.RETAINED, name, archive_path=occurrence["archive_path"],
                    kind=occurrence["kind"], state="evicting", reason=MANUAL)
    elif confirmed is None:
        report.item(Outcome.REFUSED, name, archive_path=occurrence["archive_path"],
                    kind=occurrence["kind"], state="recorded", reason=NO_PROOF)
    for observation in candidates:
        if observation.status == "orphaned":
            report.item(Outcome.ORPHANED, observation.snapshot_id,
                        archive_path=occurrence["archive_path"],
                        reason=observation.reason)


# ---- step 6: watermarks and lineage ----------------------------------------

def _rebuild_meta(db, observations, store_id: str, base) -> None:
    own = [o.identity.export_seq for o in observations
           if o.identity.store_id == store_id]
    with transaction(db):
        top = db.execute("SELECT coalesce(max(export_seq), 0)"
                         " FROM publication_attempt WHERE origin_store_id = ?",
                         (store_id,)).fetchone()[0]
        frontier = db.execute(
            "SELECT coalesce(max(export_seq), 0) FROM publication_attempt"
            " WHERE origin_store_id = ? AND outcome = 'confirmed'",
            (store_id,)).fetchone()[0]
        db.execute("UPDATE store_meta SET store_id = ?, export_seq = ?,"
                   " published_frontier = ?",
                   (store_id, max([top, *own]), frontier))
        foreign: dict[str, int] = {}
        for observation in observations:
            if observation.identity.store_id != store_id and observation.valid:
                seq = observation.identity.export_seq
                foreign[observation.identity.store_id] = max(
                    foreign.get(observation.identity.store_id, 0), seq)
        for other_store, merged_through in foreign.items():
            records.merge_lineage(db, other_store, merged_through)


def _counts(db) -> dict[str, int]:
    counts = {row["state"]: row["n"] for row in db.execute(
        "SELECT state, count(*) AS n FROM occurrence GROUP BY state")}
    counts["orphaned_snapshots"] = db.execute(
        "SELECT count(*) FROM snapshot WHERE status = 'orphaned'").fetchone()[0]
    counts["unresolved_attempts"] = db.execute(
        "SELECT count(*) FROM publication_attempt WHERE outcome = 'pending'"
    ).fetchone()[0]
    return counts


# ---- step 7: publish -------------------------------------------------------

def _publish(db, work_store: Path, store_path: Path, state_dir: Path) -> None:
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db.execute("PRAGMA journal_mode = DELETE")
    db.close()
    for sidecar in (f"{work_store}-wal", f"{work_store}-shm"):
        if os.path.exists(sidecar):
            os.unlink(sidecar)
    _fsync(work_store)
    _fsync(work_store.parent)
    os.replace(work_store, store_path)
    _fsync(state_dir)


def _fsync(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _report_summary(report: Report, counts, observations, skipped) -> None:
    summary = ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))
    report.item(Outcome.INFO, "recovered",
                reason=f"{summary}; snapshots observed={len(observations)}, "
                       f"skipped={len(skipped)}")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class _closing:
    def __init__(self, connection) -> None:
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, *exc) -> None:
        self.connection.close()
