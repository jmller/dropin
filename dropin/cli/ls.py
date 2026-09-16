"""Recent occurrences; explicit --state exposes the operational view."""

import json

from ..config import ConfigError
from ..query.filters import QueryError
from ..query.search import ls, read_store


def run(context, args):
    try:
        with read_store(context.config) as db:
            for row in ls(db, since=args.since, limit=args.limit if args.limit is not None else 100, state=args.state):
                print(json.dumps(row, ensure_ascii=True) if context.json_output
                      else f"{row['state']}\t{row['archive_path']}")
        return 0
    except QueryError as error:
        raise ConfigError(str(error)) from error
