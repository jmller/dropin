"""The three MCP tool schemas and their shared local-query bindings."""

import sqlite3

from ..config import ConfigError
from ..query.filters import Filters, QueryError, string, validate_show_target
from ..query.search import find, read_store, show

STRING = {"type": "string", "minLength": 1}
DATE = {"type": "string", "description": "ISO date or timestamp; absent timezone is UTC"}
HASH = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
INTEGER = {"type": "integer", "minimum": 0, "maximum": 2**63 - 1}


def _schema(properties, **extra):
    return {"type": "object", "properties": properties, "additionalProperties": False, **extra}


# Keep these schemas within the simple object/property subset supported by MCP
# clients, notably Codex CLI.  The server performs the richer cross-field checks
# below in Filters and validate_show_target; expressing them with root-level
# allOf/oneOf causes Codex to expose the arguments as ``unknown`` and then send
# an invalid tools/call envelope.
FIND_SCHEMA = _schema({
    **{key: STRING for key in ("name", "glob", "uti", "kind", "text")},
    **{key: DATE for key in ("created_since", "created_until", "modified_since", "modified_until")},
    "tags": {"type": "array", "items": STRING}, "size_min": INTEGER, "size_max": INTEGER,
    "sha256": HASH, "limit": {**INTEGER, "minimum": 1, "default": 100},
    "include_attributes": {"type": "boolean", "default": False},
})
SHOW_SCHEMA = _schema({"archive_path": STRING, "sha256": HASH},
                      description="Provide exactly one of archive_path or sha256.")
GET_SCHEMA = _schema({"archive_path": STRING, "destination_dir": STRING,
                     "force": {"type": "boolean", "default": False}}, required=["archive_path", "destination_dir"])
TOOLS = [
    {"name": "find", "description": "Search confirmed archive entries locally; kind is an exact UTI alias.", "inputSchema": FIND_SCHEMA},
    {"name": "show", "description": "Read full captured metadata locally, by path or hash.", "inputSchema": SHOW_SCHEMA},
    {"name": "get", "description": "Restore with verification to an existing destination directory.", "inputSchema": GET_SCHEMA},
]
SCHEMAS = {tool["name"]: tool["inputSchema"] for tool in TOOLS}


def _validate(name, arguments):
    if name not in SCHEMAS:
        raise QueryError(f"unknown tool: {name}")
    if not isinstance(arguments, dict):
        raise QueryError("arguments must be an object")
    schema = SCHEMAS[name]
    for key, value in arguments.items():
        if key not in schema["properties"]:
            raise QueryError(f"unknown {name} argument: {key}")
        expected = schema["properties"][key]["type"]
        valid = {"string": isinstance(value, str), "boolean": type(value) is bool,
                 "integer": type(value) is int, "array": isinstance(value, list)}[expected]
        if not valid:
            raise QueryError(f"{key} must be {expected}")
        if expected == "string":
            string(value, key)
    for key in schema.get("required", []):
        if key not in arguments:
            raise QueryError(f"missing {key}")


def call(config, name, arguments):
    _validate(name, arguments)
    if name == "show":
        validate_show_target(**arguments)
    try:
        if name == "get":
            return _get(config, arguments)
        if name == "find":
            parameters = dict(arguments)
            include_attributes = parameters.pop("include_attributes", False)
            filters = Filters(**parameters)
            with read_store(config) as db:
                return list(find(db, filters, include_attributes=include_attributes)), False
        with read_store(config) as db:
            return show(db, **arguments), False
    except LookupError as error:
        return {"error": "missing", "reason": str(error)}, True
    except (ConfigError, sqlite3.DatabaseError, OSError) as error:
        return {"error": "store", "reason": str(error)}, True


def _get(config, arguments):
    # Retrieval alone may construct the engine/run its gate. Query tools remain
    # independent of this path, and no tool uses writable Context.db or a lock.
    from .. import cli
    from ..retrieve import RetrieveError, resolve, retrieve

    try:
        with read_store(config) as db:
            resolve(db, arguments['archive_path'])
            context = cli.Context(config)
            reason = cli.tools_gate(context)
            if reason:
                raise RetrieveError('tools', reason)
            result = retrieve(
                db, context.engine, arguments['archive_path'],
                arguments['destination_dir'], force=arguments.get('force', False))
            return result, False
    except RetrieveError as error:
        return {'error': error.kind, 'reason': str(error)}, True
