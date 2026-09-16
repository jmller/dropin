"""Validated, parameterized local query predicates."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import fnmatch
import re


class QueryError(ValueError):
    """Invalid query input; CLI usage / MCP invalid params, not an empty result."""


HASH = re.compile(r"[0-9a-f]{64}\Z")
DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)?\Z")
MAX_INTEGER = 2**63 - 1
STATES = ("recorded", "transferred", "verified", "recoverable", "evicting", "evicted", "abandoned")


def string(value, key):
    if not isinstance(value, str) or not value:
        raise QueryError(f"{key} must be a nonempty string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise QueryError(f"{key} must be valid Unicode") from error
    return value


def integer(value, key, minimum=0):
    if type(value) is not int or not minimum <= value <= MAX_INTEGER:
        raise QueryError(f"{key} must be an integer in {minimum}..{MAX_INTEGER}")
    return value


def validate_sha256(value):
    if not isinstance(value, str) or not HASH.fullmatch(value):
        raise QueryError("sha256 must be 64 lowercase hexadecimal characters")
    return value


def validate_show_target(*, archive_path=None, sha256=None):
    if (archive_path is None) == (sha256 is None):
        raise QueryError("show requires exactly one of archive_path or sha256")
    if sha256 is not None:
        validate_sha256(sha256)
    else:
        string(archive_path, "archive_path")


def date_bound(value, *, upper=False):
    """Return a precision-stable UTC key and whether the bound is exclusive."""
    string(value, "date")
    try:
        if DATE.fullmatch(value):
            day = date.fromisoformat(value)
            if upper:
                day += timedelta(days=1)
            parsed = datetime.combine(day, datetime.min.time(), timezone.utc)
            exclusive = upper
        elif TIMESTAMP.fullmatch(value):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            parsed = parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
            exclusive = False
        else:
            raise ValueError("expected ISO date or timestamp")
        return parsed.isoformat(timespec="microseconds"), exclusive
    except (ValueError, OverflowError) as error:
        raise QueryError(f"invalid date {value!r}: {error}") from error


def _stored_date(value):
    if value is None:
        return None
    try:
        return date_bound(value)[0]
    except QueryError:
        # Captured unknown/malformed metadata cannot satisfy a date predicate.
        return None


def configure(connection):
    connection.create_function("query_date", 1, _stored_date, deterministic=True)
    connection.create_function("query_name", 2,
                               lambda name, part: name is not None and part.casefold() in name.casefold(),
                               deterministic=True)
    connection.create_function("query_glob", 2,
                               lambda name, pattern: name is not None and fnmatch.fnmatchcase(name, pattern),
                               deterministic=True)


def date_predicates(column, lower, upper):
    clauses, values = [], []
    start = date_bound(lower)[0] if lower is not None else None
    end, exclusive = date_bound(upper, upper=True) if upper is not None else (None, False)
    if start is not None and end is not None and (start >= end if exclusive else start > end):
        raise QueryError("date lower bound is after upper bound")
    if start is not None:
        clauses.append(f"query_date({column}) >= ?")
        values.append(start)
    if end is not None:
        clauses.append(f"query_date({column}) {'<' if exclusive else '<='} ?")
        values.append(end)
    return clauses, values


@dataclass(frozen=True)
class Filters:
    name: str | None = None
    glob: str | None = None
    uti: str | None = None
    kind: str | None = None
    created_since: str | None = None
    created_until: str | None = None
    modified_since: str | None = None
    modified_until: str | None = None
    tags: list[str] | tuple[str, ...] = ()
    size_min: int | None = None
    size_max: int | None = None
    sha256: str | None = None
    text: str | None = None
    limit: int = 100

    def __post_init__(self):
        for key in ("name", "glob", "uti", "kind", "text"):
            value = getattr(self, key)
            if value is not None:
                string(value, key)
        for first, second in (("name", "glob"), ("kind", "uti")):
            if getattr(self, first) is not None and getattr(self, second) is not None:
                raise QueryError(f"{first} and {second} are mutually exclusive")
        integer(self.limit, "limit", 1)
        for key in ("size_min", "size_max"):
            if getattr(self, key) is not None:
                integer(getattr(self, key), key)
        if self.size_min is not None and self.size_max is not None and self.size_min > self.size_max:
            raise QueryError("size_min is greater than size_max")
        if not isinstance(self.tags, (list, tuple)):
            raise QueryError("tags must be an array of strings")
        for tag in self.tags:
            string(tag, "tag")
        if self.sha256 is not None:
            validate_sha256(self.sha256)
        date_predicates("n.created", self.created_since, self.created_until)
        date_predicates("n.modified", self.modified_since, self.modified_until)

    def sql(self):
        clauses = ["o.confirmed_attempt_id IS NOT NULL", "e.searchable = 1"]
        values = []
        for field, expression in (("name", "query_name(n.name, ?)"), ("glob", "query_glob(n.name, ?)"),
                                  ("uti", "n.uti = ?"), ("kind", "n.uti = ?"),
                                  ("size_min", "n.size_bytes >= ?"), ("size_max", "n.size_bytes <= ?"),
                                  ("sha256", "n.sha256 = ?")):
            value = getattr(self, field)
            if value is not None:
                clauses.append(expression)
                values.append(value)
        for column, lower, upper in (("n.created", self.created_since, self.created_until),
                                     ("n.modified", self.modified_since, self.modified_until)):
            date_clauses, date_values = date_predicates(column, lower, upper)
            clauses.extend(date_clauses)
            values.extend(date_values)
        for tag in self.tags:
            clauses.append("EXISTS (SELECT 1 FROM tag t WHERE t.occ_id=e.occ_id AND t.rel_path=e.rel_path AND t.tag=?)")
            values.append(tag)
        if self.text is not None:
            clauses.append("n.rowid IN (SELECT rowid FROM fulltext WHERE fulltext MATCH ?)")
            values.append(self.text)
        return " AND ".join(clauses), values
