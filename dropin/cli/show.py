"""Full captured metadata by archive path or SHA-256."""

import json
import sys

from ..config import ConfigError
from ..query.filters import HASH, QueryError
from ..query.search import read_store, show


def run(context, args):
    try:
        with read_store(context.config) as db:
            result = show(db, **({"sha256": args.target} if HASH.fullmatch(args.target)
                                 else {"archive_path": args.target}))
        if context.json_output:
            for record in result if isinstance(result, list) else [result]:
                print(json.dumps(record, ensure_ascii=True))
        else:
            print(json.dumps(result, ensure_ascii=True, indent=2))
        return 0
    except QueryError as error:
        raise ConfigError(str(error)) from error
    except LookupError as error:
        print(f"dropin: {error}", file=sys.stderr)
        return 1
