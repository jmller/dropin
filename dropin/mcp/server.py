"""Newline-delimited JSON-RPC 2.0 over stdio, without an SDK."""

import json
import math
import sys

from .. import __version__
from ..query.filters import QueryError
from ..report import diagnostic_secrets, redact_payload
from .tools import TOOLS, call


def _error(id, code, message):
    return {"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": message}}


def _dispatch(config, method, params):
    if method not in ("initialize", "ping", "tools/list", "tools/call"):
        raise LookupError(f"unknown method: {method}")
    if not isinstance(params, dict):
        raise QueryError("params must be an object")
    if method == "initialize":
        return {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                "serverInfo": {"name": "dropin", "version": __version__}}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        # MCP clients may attach reserved request metadata (for example Codex's
        # progress and call identifiers). It is transport metadata, not a tool
        # argument, and must not make an otherwise valid invocation fail.
        if set(params) - {"name", "arguments", "_meta"} or not isinstance(params.get("name"), str):
            raise QueryError("tools/call requires a tool name and optional arguments object")
        payload, is_error = call(config, params["name"], params.get("arguments", {}))
        return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=True)}], "isError": is_error}


def response(config, message):
    if (not isinstance(message, dict) or message.get("jsonrpc") != "2.0"
            or not isinstance(message.get("method"), str)
            or ("id" in message and message["id"] is not None and type(message["id"]) not in (str, int, float))):
        return _error(None, -32600, "invalid JSON-RPC request")
    if "id" not in message:
        # JSON-RPC notifications (including initialized) never get a response.
        return None
    id = message["id"]
    secrets = diagnostic_secrets(config)
    try:
        result = _dispatch(config, message["method"], message.get("params", {}))
        payload = {"jsonrpc": "2.0", "id": id, "result": result}
    except QueryError as error:
        payload = _error(id, -32602, str(error))
    except LookupError as error:
        payload = _error(id, -32601, str(error))
    return redact_payload(payload, secrets)


def _nonfinite(value):
    raise ValueError(f"invalid JSON constant: {value}")


def _finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("JSON number is outside finite range")
    return number


def serve(config, stdin=None, stdout=None):
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    for line in stdin:
        try:
            message = json.loads(line, parse_constant=_nonfinite, parse_float=_finite_float)
        except (ValueError, UnicodeError, RecursionError):
            result = _error(None, -32700, "malformed JSON")
        else:
            result = response(config, message)
        if result is not None:
            stdout.write(json.dumps(result, ensure_ascii=True) + "\n")
            stdout.flush()
    return 0
