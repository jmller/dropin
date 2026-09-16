"""Archive paths (or full records) from the local index."""

import json
import sys

from ..config import ConfigError
from ..query.filters import Filters, QueryError
from ..query.search import find, json_record, read_store


def run(context, args):
    try:
        filters = Filters(name=args.name, glob=args.glob, kind=args.kind, uti=args.uti,
                          created_since=args.since, created_until=args.until,
                          modified_since=args.modified_since, modified_until=args.modified_until,
                          tags=args.tag, size_min=args.size_min, size_max=args.size_max,
                          sha256=args.hash, text=args.text, limit=args.limit if args.limit is not None else 100)
        with read_store(context.config) as db:
            for record in find(db, filters, full=context.json_output):
                if context.json_output:
                    print(json.dumps(json_record(record), ensure_ascii=True))
                else:
                    sys.stdout.write(record["archive_path"] + ("\0" if context.nul_separated else "\n"))
        return 0
    except QueryError as error:
        raise ConfigError(str(error)) from error
