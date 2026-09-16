"""Thin CLI adapter over the read-only verified restore transaction."""
import itertools
import json
import sys

from . import Context
from .. import cli
from ..query.search import read_store
from ..report import Outcome, OutcomeRecord
from ..retrieve import RetrieveError, resolve, retrieve
from ..store.records import new_run_id


def _paths(paths, nul):
    for path in paths:
        if path != '-':
            yield path
            continue
        separator = b'\0' if nul else b'\n'
        pending = b''
        for chunk in iter(lambda: sys.stdin.buffer.read(65536), b''):
            parts = (pending + chunk).split(separator)
            pending = parts.pop()
            for part in parts:
                if part:
                    yield part.decode('utf-8', 'surrogateescape')
        if pending:
            yield pending.decode('utf-8', 'surrogateescape')


def error_exit(kind, attempted=False):
    if kind == 'usage':
        return 2
    if kind in ('corrupt', 'missing'):
        return 4
    if kind in ('no-repo', 'tool-error', 'locked', 'bad-password', 'tools'):
        return 1 if attempted else 3
    return 1


def run(context: Context, args):
    if not args.paths or args.paths.count('-') > 1:
        print('dropin: get requires paths or one stdin -', file=sys.stderr)
        return 2
    paths = _paths(args.paths, context.nul_separated)
    if args.stdout:
        selected = list(itertools.islice(paths, 2))
        if len(selected) != 1:
            print('dropin: --stdout requires a single path', file=sys.stderr)
            return 2
        paths = iter(selected)
    run_id = new_run_id()
    exit_code, attempted, gated = 0, False, False
    with read_store(context.config) as db:
        for path in paths:
            result, target, error_kind, code = {}, None, None, None
            try:
                target = resolve(db, path)
                if args.stdout and target.kind != 'file':
                    raise RetrieveError('usage', '--stdout requires one regular file')
                destination = args.destination or '.'
                if not gated:
                    reason = cli.tools_gate(context)
                    if reason:
                        print(f'dropin: get refused: {reason}', file=sys.stderr)
                        return 3
                    gated = True
                result = retrieve(
                    db, context.engine, path, destination, force=args.force,
                    stdout=sys.stdout.buffer if args.stdout else None)
                outcome, reason = Outcome.RESTORED, None
            except RetrieveError as error:
                code = error_exit(error.kind, attempted)
                exit_code = 4 if 4 in (exit_code, code) else max(exit_code, code)
                outcome = Outcome.CORRUPT if error.kind == 'corrupt' else Outcome.MISSING if error.kind == 'missing' else Outcome.REFUSED
                error_kind = error.kind
                reason = f'{error_kind}: {error}'
                if args.stdout or code == 2:
                    print(f'dropin: {path}: {reason}', file=sys.stderr)
                    return exit_code
            attempted = True
            if args.stdout:
                continue
            record = OutcomeRecord('get', outcome, target.name if target else path,
                run_id, archive_path=path, kind=target.kind if target else None,
                state=target.occurrence['state'] if target else None,
                snapshot=target.snapshot if target else None,
                sha256=result.get('sha256'), size=target.entry['size_bytes'] if target else None,
                reason=reason)
            if context.json_output:
                payload = {**record.to_dict(), **result}
                if error_kind is not None:
                    payload['error'] = error_kind
                print(json.dumps(payload, ensure_ascii=True))
            else:
                print(record.to_human(), file=sys.stderr if reason else sys.stdout)
                if result.get('aside_path'):
                    print(f"dropin: previous destination retained at {result['aside_path']}", file=sys.stderr)
            if code == 3:
                return exit_code
    return exit_code
