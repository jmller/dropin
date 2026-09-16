"""Connection, migrations, and the transaction helper.

`PRAGMA user_version` drives migrations: each `schema/NNNN_*.sql` is applied in
order, inside one transaction each, and the version is bumped in the same
transaction so a crash mid-migration leaves the previous version intact.
"""

from __future__ import annotations

from contextlib import contextmanager
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path
import re
import sqlite3

# A Traversable works both from a checkout/wheel and from the pip-free zipapp.
SCHEMA_DIR = files("dropin.store.schema")
_SCHEMA_NAME = re.compile(r"^[0-9]{4}_.+\.sql$")


def _migrations() -> list[tuple[int, Traversable]]:
    found = []
    for path in sorted(SCHEMA_DIR.iterdir(), key=lambda item: item.name):
        if _SCHEMA_NAME.fullmatch(path.name):
            found.append((int(path.name[:4]), path))
    return found


#: The version a freshly created store reports.
SCHEMA_VERSION = max((version for version, _ in _migrations()), default=0)


class StoreCompatibilityError(sqlite3.DatabaseError):
    """The store was written by a newer, unsupported Dropin schema."""


def _require_supported_schema(connection: sqlite3.Connection) -> None:
    current = connection.execute("PRAGMA user_version").fetchone()[0]
    if current > SCHEMA_VERSION:
        raise StoreCompatibilityError(
            f"store schema {current} is newer than supported schema "
            f"{SCHEMA_VERSION}; use a compatible Dropin version")


def connect(path: Path | str, *, read_only: bool = False) -> sqlite3.Connection:
    """Open (and migrate) a store. Rows come back as mappings."""
    path = Path(path)
    if read_only:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            _require_supported_schema(connection)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            return connection
        except BaseException:
            connection.close()
            raise

    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        _require_supported_schema(connection)
    except BaseException:
        connection.close()
        raise
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA synchronous = FULL")
    migrate(connection)
    return connection


def migrate(connection: sqlite3.Connection) -> int:
    current = connection.execute("PRAGMA user_version").fetchone()[0]
    for version, path in _migrations():
        if version <= current:
            continue
        # `executescript` commits any open transaction before it runs, so the
        # BEGIN/COMMIT pair goes *inside* the script: the schema and its version
        # bump land together or not at all.
        connection.executescript(
            "BEGIN;\n"
            f"{path.read_text()}\n"
            f"PRAGMA user_version = {version};\n"
            "COMMIT;\n")
        current = version
    return current


@contextmanager
def transaction(connection: sqlite3.Connection):
    """One durable unit of work. Every state transition uses this."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    connection.execute("COMMIT")
