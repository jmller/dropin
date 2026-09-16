"""Explicit initialization and inspection of local restore request history."""

from __future__ import annotations

import json
import sys

from ..report import EXIT_OK, EXIT_RUN_REFUSED, EXIT_USAGE
from ..restore_state import RestoreStateError, initialize, inspect


def run(context, args) -> int:
    try:
        if args.restore_state_action == "init":
            profile = initialize(context.config)
            _emit(context, profile.to_dict())
            return EXIT_OK
        profile = inspect(context.config)
        _emit(context, profile.to_dict())
        # Schema v1 deliberately reports attention until strict J/P and the
        # deployment assumptions have independent acceptance.
        return EXIT_OK if profile.activation == "enabled" else 1
    except RestoreStateError as error:
        print(f"dropin: restore-state {args.restore_state_action} refused: {error}",
              file=sys.stderr)
        return EXIT_USAGE if error.kind == "usage" else EXIT_RUN_REFUSED


def _emit(context, payload: dict) -> None:
    if context.json_output:
        print(json.dumps(payload, sort_keys=True, ensure_ascii=True))
        return
    print(f"generation\t{payload['generation']}")
    print(f"activation\t{payload['activation']}")
    for item in payload["destinations"]:
        print(f"destination\t{item['destination_id']}\t{item['path']}")
    for gate in payload["blocked_gates"]:
        print(f"blocked\t{gate}")
