"""The stale-store and lineage gate.

Before publishing anything, every `dropin:v=1` snapshot in the repository must be
one this store knows about. A snapshot it has never seen means the local
database is behind the repository — typically a store restored from an old
backup — and publishing on top of that would allocate sequences that already
exist and hide the history in between.

Two ways to be known, and only two:

* an exact `snapshot` ledger observation (id plus canonical tag set), or
* a local `publication_attempt` whose six canonical tag values match.

The second deliberately does **not** require a recorded snapshot id. The attempt
row is committed before `restic backup` runs, so a crash before the id is
captured leaves it null by design; demanding one here would refuse every
subsequent run and brick the store — the exact deadlock this gate was rewritten
to fix. When the gate matches that way it records the observation
and moves on: settling the attempt is the owning occurrence's decision, not the
gate's.

Neither `export_seq` nor `published_frontier` is ever the comparator. They are
watermarks; a watermark cannot tell you whether a particular snapshot is yours.
"""

from __future__ import annotations

import json

from ..engine.interface import TagError, parse_tags
from ..store import records
from ..store.db import transaction

RECOVERY_GUIDANCE = (
    "preserve this state directory, run `dropin recover --into <new empty "
    "state dir>`, inspect the result, then point the configuration at it")


class StaleStore(Exception):
    """The repository holds a snapshot this store has never seen."""


class LineageMismatch(Exception):
    """A snapshot from another store, not adopted into this lineage."""


def check_frontier(connection, engine, store_id: str, *, observe: bool = True) -> None:
    known_lineage = records.lineage(connection)
    for snapshot in engine.snapshots(tag="dropin:v=1"):
        try:
            identity = parse_tags(snapshot.tags)
        except TagError as error:
            raise StaleStore(
                f"snapshot {snapshot.id} has malformed reserved tags ({error}); "
                f"refusing to publish. {RECOVERY_GUIDANCE}") from error

        if identity.store_id != store_id:
            merged_through = known_lineage.get(identity.store_id)
            if merged_through is None or identity.export_seq > merged_through:
                raise LineageMismatch(
                    f"snapshot {snapshot.id} belongs to store "
                    f"{identity.store_id} at sequence {identity.export_seq}, "
                    f"which is not merged into this lineage; use "
                    f"`drain --adopt-lineage` to validate and merge it first")
            continue

        _require_known(connection, snapshot, identity, observe=observe)


def assess_frontier(connection, engine, store_id: str) -> None:
    """Apply the complete identity gate without changing the observation ledger."""
    check_frontier(connection, engine, store_id, observe=False)


def _require_known(connection, snapshot, identity, *, observe: bool) -> None:
    observed = records.get_snapshot(connection, snapshot.id)
    if observed is not None:
        if json.loads(observed["tag_set_json"]) != identity.tag_set():
            raise StaleStore(
                f"snapshot {snapshot.id} carries tags this store recorded "
                f"differently; {RECOVERY_GUIDANCE}")
        return

    attempt = _matching_attempt(connection, identity)
    if attempt is None:
        raise StaleStore(
            f"snapshot {snapshot.id} (sequence {identity.export_seq}) is not "
            f"known to this store, whose highest sequence is "
            f"{records.store_meta(connection)['export_seq']}; "
            f"{RECOVERY_GUIDANCE}")

    # Known by its attempt row, not yet ledgered. Publication records the
    # observation; observational status only reports that the identity passes.
    if observe:
        with transaction(connection):
            records.observe_snapshot(connection, snapshot.id, identity,
                                     status="pending")


def _matching_attempt(connection, identity):
    """Match on the six canonical tag values; a null snapshot_id is expected."""
    row = connection.execute(
        "SELECT a.*, o.kind FROM publication_attempt a"
        " JOIN occurrence o ON o.occ_id = a.occ_id"
        " WHERE a.attempt_id = ? AND a.occ_id = ? AND a.origin_store_id = ?"
        " AND a.export_seq = ?",
        (identity.attempt_id, identity.occ_id, identity.store_id,
         identity.export_seq)).fetchone()
    if row is None:
        return None
    if row["kind"] != identity.kind:
        return None
    if row["export_sha256"] not in (None, identity.catalog_sha256):
        return None
    return row
