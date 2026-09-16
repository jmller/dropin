"""`python3 -m dropin`: argparse dispatch and run context.

Every verb is registered here so `--help` is the contract, and the writer lock
is applied from one table rather than per verb — a mutating verb that forgot to
lock would be a silent single-writer violation.
"""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .cli import Context
from .config import ConfigError, load
from .report import EXIT_RUN_REFUSED, EXIT_USAGE

#: verbs that may mutate the store or the repository.
WRITER_VERBS = frozenset({"init", "drain", "recover", "unlock", "verify",
                          "restore-state", "uninstall"})

VERBS = ("init", "setup", "uninstall", "drain", "add", "find", "show", "ls", "get", "verify",
         "status", "recover", "unlock", "restore-state", "mcp")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dropin", description="Folderless drop-in archiver.")
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {__version__}")
    parser.add_argument("--config", help="configuration file "
                                         "(default: $DROPIN_CONFIG, then "
                                         "~/.config/dropin/config.toml)")
    parser.add_argument("--json", action="store_true", dest="json_output",
                        help="emit NDJSON records on stdout")
    parser.add_argument("-0", action="store_true", dest="nul_separated",
                        help="NUL-separate paths on stdin/stdout")
    subparsers = parser.add_subparsers(dest="verb", metavar="verb")

    init = subparsers.add_parser("init", help="create config, store and repository")
    init.add_argument("--repo")
    init.add_argument("--drop-dir")
    init.add_argument("--state-dir")
    init.add_argument("--password-file")
    init.add_argument("--launchd", action="store_true")
    init.add_argument("--label")
    init.add_argument("--interval", type=int)
    init.add_argument("--force", action="store_true")

    setup = subparsers.add_parser(
        "setup", help="guided first-time configuration and repository setup")
    setup.add_argument("--repo", help="repository URL (otherwise prompt)")
    setup.add_argument("--drop-dir", help="drop directory (default: ~/Drop)")
    setup.add_argument("--state-dir", help="state directory (default: ~/.local/state/dropin)")
    setup.add_argument("--password-file")
    setup.add_argument("--launchd", action="store_true")
    setup.add_argument("--label")
    setup.add_argument("--interval", type=int)
    setup.add_argument("--force", action="store_true")

    uninstall = subparsers.add_parser(
        "uninstall", help="remove Dropin; --purge also removes local data")
    uninstall.add_argument("--purge", action="store_true",
                           help="remove configuration, password, state, and launch agent")
    uninstall.add_argument("--yes", action="store_true",
                           help="confirm the destructive purge")
    uninstall.add_argument("--purge-drop", action="store_true",
                           help="also remove the configured drop directory")
    uninstall.add_argument("--binary", help="standalone Dropin executable to remove")
    uninstall.add_argument("--dry-run", action="store_true")

    drain = subparsers.add_parser("drain", help="archive everything in the spool")
    drain.add_argument("--dry-run", action="store_true")
    drain.add_argument("--adopt-lineage", action="store_true")
    drain.add_argument("--retry-exhausted", action="store_true")
    drain.add_argument("--settle", type=float)

    add = subparsers.add_parser("add", help="move paths into the spool")
    add.add_argument("paths", nargs="*")

    find = subparsers.add_parser("find", help="query archived records")
    names = find.add_mutually_exclusive_group()
    names.add_argument("--name")
    names.add_argument("--glob")
    types = find.add_mutually_exclusive_group()
    types.add_argument("--kind")
    types.add_argument("--uti")
    find.add_argument("--json", action="store_true", dest="json_output", default=argparse.SUPPRESS)
    find.add_argument("-0", action="store_true", dest="nul_separated", default=argparse.SUPPRESS)
    find.add_argument("--since")
    find.add_argument("--until")
    find.add_argument("--modified-since")
    find.add_argument("--modified-until")
    find.add_argument("--tag", action="append", default=[])
    find.add_argument("--size-min", type=int)
    find.add_argument("--size-max", type=int)
    find.add_argument("--hash")
    find.add_argument("--text")
    find.add_argument("--limit", type=int)

    show = subparsers.add_parser("show", help="full record for one archive path")
    show.add_argument("target")

    listing = subparsers.add_parser("ls", help="recent occurrences")
    listing.add_argument("--since")
    listing.add_argument("--limit", type=int)
    listing.add_argument("--state")

    get = subparsers.add_parser("get", help="restore by archive path")
    get.add_argument("paths", nargs="*")
    get.add_argument("-o", dest="destination")
    get.add_argument("--stdout", action="store_true")
    get.add_argument("--force", action="store_true")

    verify = subparsers.add_parser("verify", help="re-read and check archived data")
    verify.add_argument("paths", nargs="*")
    verify.add_argument("--all", action="store_true")
    verify.add_argument("--repo", action="store_true")
    verify.add_argument("--subset")
    verify.add_argument("--since")

    status = subparsers.add_parser("status", help="report health and counts")
    status.add_argument("--json", action="store_true", dest="json_output",
                        default=argparse.SUPPRESS)
    status.add_argument("--offline", action="store_true")

    recover = subparsers.add_parser("recover", help="rebuild a store from the repository")
    recover.add_argument("--into", required=True)
    recover.add_argument("--repo")
    recover.add_argument("--password-file")
    recover.add_argument("--trust-later-exports", action="store_true")

    subparsers.add_parser("unlock", help="remove a stale repository lock")

    restore_state = subparsers.add_parser(
        "restore-state", help="initialize or inspect reserved restore history")
    restore_state.add_argument("restore_state_action", choices=("init", "status"))

    subparsers.add_parser("mcp", help="serve find/show/get over stdio JSON-RPC")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    if not args.verb:
        parser.print_usage(sys.stderr)
        print("dropin: a verb is required", file=sys.stderr)
        return EXIT_USAGE

    if args.verb in ("init", "setup", "uninstall"):
        # These verbs run before a configuration exists: they write or remove
        # the file the other verbs load.
        module = {"init": "init", "setup": "setup", "uninstall": "uninstall"}[args.verb]
        run_setup = __import__(f"dropin.cli.{module}", fromlist=["run"]).run
        return run_setup(args)

    try:
        config = load(args.config)
    except ConfigError as error:
        print(f"dropin: {error}", file=sys.stderr)
        return EXIT_USAGE

    context = Context(config=config, json_output=args.json_output,
                      nul_separated=args.nul_separated)
    handler = _handler(args.verb)
    if handler is None:
        print(f"dropin: {args.verb} is not implemented yet", file=sys.stderr)
        return EXIT_USAGE

    try:
        if args.verb in WRITER_VERBS:
            from .pipeline.writer_lock import LockHeld, writer_lock

            try:
                with writer_lock(config.writer_lock_path, verb=args.verb):
                    return handler(context, args)
            except LockHeld as error:
                print(f"dropin: {error}", file=sys.stderr)
                return EXIT_RUN_REFUSED
        return handler(context, args)
    except ConfigError as error:
        print(f"dropin: {error}", file=sys.stderr)
        return EXIT_USAGE


def _handler(verb: str):
    """Resolve a verb to its module. Missing modules mean 'not implemented'."""
    try:
        module = __import__(f"dropin.cli.{verb.replace('-', '_')}", fromlist=["run"])
    except ImportError:
        return None
    return getattr(module, "run", None)


if __name__ == "__main__":
    raise SystemExit(main())
