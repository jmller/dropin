"""Local-only readers and lossless query presentation.

No Context.db: query connections must never initialize/migrate a store. Each
CLI invocation / MCP call sees one committed SQLite read snapshot across its
entry, attribute and manifest reads, even while drain commits newer states.
"""

from contextlib import contextmanager
import base64
import json
import sqlite3

from ..config import ConfigError
from .filters import (Filters, QueryError, STATES, configure, date_predicates,
                      integer, validate_show_target)


@contextmanager
def read_store(config):
    if not config.store_path.is_file():
        raise ConfigError(f"no store at {config.store_path}; run `dropin init` first")
    try:
        db = sqlite3.connect(config.store_path.resolve().as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error as error:
        raise ConfigError(f"cannot open local store: {error}") from error
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        yield db
    except sqlite3.Error as error:
        raise ConfigError(f"cannot read local store: {error}") from error
    finally:
        db.close()


def json_record(record):
    """Keep surrogateescaped names reversible, without invalid UTF-8 on wire."""
    result = dict(record)
    name = result.get("name")
    if isinstance(name, str) and any(0xDC80 <= ord(char) <= 0xDCFF for char in name):
        result["name_b64"] = base64.b64encode(name.encode("utf-8", "surrogateescape")).decode("ascii")
    return result


ENTRY_QUERY = """SELECT e.occ_id, e.rel_path FROM entry e
 JOIN occurrence o ON o.occ_id=e.occ_id
 JOIN normalized n ON n.occ_id=e.occ_id AND n.rel_path=e.rel_path"""
ORDER = " ORDER BY o.recorded_at DESC, e.archive_path ASC"


def find(db, filters=None, *, full=False, include_attributes=False):
    filters = filters or Filters()
    configure(db)
    where, values = filters.sql()
    if filters.text is not None:
        try:
            # Validate MATCH independently of other predicates, even on no hits.
            db.execute("SELECT rowid FROM fulltext WHERE fulltext MATCH ? LIMIT 1", (filters.text,)).fetchone()
        except sqlite3.OperationalError as error:
            raise QueryError(f"invalid text query: {error}") from error
    for row in db.execute(ENTRY_QUERY + " WHERE " + where + ORDER + " LIMIT ?", (*values, filters.limit)):
        yield _record(db, row["occ_id"], row["rel_path"], full=full, include_attributes=include_attributes)


def _attributes(db, occ_id, rel_path):
    rows = db.execute("""SELECT key,value_json,value_type,source FROM attribute
        WHERE occ_id=? AND rel_path=? ORDER BY key,
        CASE source WHEN 'mdls' THEN 0 WHEN 'importer' THEN 1 WHEN 'xattr' THEN 2 ELSE 3 END""", (occ_id, rel_path))
    primary, all_values = {}, []
    for row in rows:
        value = {"value": json.loads(row["value_json"]), "type": row["value_type"], "source": row["source"]}
        primary.setdefault(row["key"], value)
        all_values.append({"key": row["key"], **value})
    return {"attributes": primary, "attribute_values": all_values}


def _record(db, occ_id, rel_path, *, full, include_attributes=False):
    entry = dict(db.execute("SELECT * FROM entry WHERE occ_id=? AND rel_path=?", (occ_id, rel_path)).fetchone())
    occurrence = dict(db.execute("SELECT * FROM occurrence WHERE occ_id=?", (occ_id,)).fetchone())
    normalized = dict(db.execute("SELECT * FROM normalized WHERE occ_id=? AND rel_path=?", (occ_id, rel_path)).fetchone())
    tags = [r[0] for r in db.execute("SELECT tag FROM tag WHERE occ_id=? AND rel_path=? ORDER BY tag", (occ_id, rel_path))]
    result = {"archive_path": entry["archive_path"], "name": normalized["name"],
              "kind": normalized["kind"], "uti": normalized["uti"], "created": normalized["created"],
              "modified": normalized["modified"], "size": normalized["size_bytes"], "sha256": normalized["sha256"],
              "tags": tags, "comment": normalized["comment"], "state": occurrence["state"],
              "has_text": bool(normalized["text"])}
    if full or include_attributes:
        result.update(_attributes(db, occ_id, rel_path))
    if full:
        attempt = db.execute("SELECT snapshot_id FROM publication_attempt WHERE attempt_id=?",
                             (occurrence["confirmed_attempt_id"],)).fetchone()
        result.update(entry=entry, normalized=normalized, occurrence=occurrence,
                      snapshot=attempt[0] if attempt else None, text=normalized["text"],
                      xattrs=[dict(r) for r in db.execute("SELECT name,value_b64,status FROM xattr WHERE occ_id=? AND rel_path=? ORDER BY name", (occ_id, rel_path))])
        if entry["entry_type"] == "dir":
            # A tree record includes its manifest; a descendant directory gets
            # only its own subtree. Bundle internal paths resolve to root first.
            prefix = rel_path + "/" if rel_path else ""
            result["manifest"] = [dict(r) for r in db.execute(
                "SELECT * FROM entry WHERE occ_id=? ORDER BY rel_path", (occ_id,))
                if r["rel_path"] == rel_path or r["rel_path"].startswith(prefix)]
    return json_record(result)


def show(db, *, archive_path=None, sha256=None):
    validate_show_target(archive_path=archive_path, sha256=sha256)
    if sha256 is not None:
        return [_record(db, r["occ_id"], r["rel_path"], full=True) for r in db.execute(
            ENTRY_QUERY + " WHERE o.confirmed_attempt_id IS NOT NULL AND e.searchable=1 AND n.sha256=?" + ORDER, (sha256,))]
    row = db.execute("""SELECT e.occ_id,e.rel_path,o.kind FROM entry e
        JOIN occurrence o ON o.occ_id=e.occ_id
        WHERE e.archive_path=? AND o.confirmed_attempt_id IS NOT NULL""", (archive_path,)).fetchone()
    if row is None:
        raise LookupError(f"unknown archive path: {archive_path}")
    return _record(db, row["occ_id"], "" if row["kind"] == "bundle" else row["rel_path"], full=True)


def ls(db, *, since=None, limit=100, state=None):
    integer(limit, "limit", 1)
    if state is not None and state not in STATES:
        raise QueryError(f"unknown state: {state}")
    configure(db)
    clauses, values = date_predicates("o.recorded_at", since, None)
    if state is None:
        clauses.append("o.confirmed_attempt_id IS NOT NULL")
    else:
        clauses.append("o.state=?")
        values.append(state)
    for row in db.execute("SELECT o.*,a.snapshot_id AS snapshot FROM occurrence o LEFT JOIN publication_attempt a ON a.attempt_id=o.confirmed_attempt_id WHERE " +
                          " AND ".join(clauses) + " ORDER BY o.recorded_at DESC,o.archive_path ASC LIMIT ?", (*values, limit)):
        yield json_record({**dict(row), "name": row["item_name"]})
