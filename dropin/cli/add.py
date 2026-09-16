"""`dropin add FILE... | -`: enqueue by rename, never transfer.

A rename is atomic on one volume and impossible across volumes; the archiver
does not copy, because a copy would be a second original the eviction gates
know nothing about. Anything that cannot be renamed is refused, per path.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path
import sys

from ..report import EXIT_USAGE, Outcome, Report
from ..store import records
from . import emit


def run(context, args) -> int:
    report = Report(verb="add", run_id=records.new_run_id())
    paths = list(args.paths)
    if paths == ["-"]:
        raw = sys.stdin.buffer.read()
        separator = b"\0" if context.nul_separated else b"\n"
        paths = [os.fsdecode(part) for part in raw.split(separator) if part]
    elif "-" in paths:
        print("dropin add: '-' must be the only path argument", file=sys.stderr)
        return EXIT_USAGE
    if not paths:
        print("dropin add: no paths given", file=sys.stderr)
        return EXIT_USAGE

    config = context.config
    for raw_path in paths:
        source = Path(raw_path)
        name = source.name or raw_path
        if not os.path.lexists(source):
            report.item(Outcome.REFUSED, name, reason=f"missing: {source}")
            continue
        resolved = Path(os.path.realpath(source))
        if resolved == config.state_dir or config.state_dir in resolved.parents:
            report.item(Outcome.REFUSED, name,
                        reason="refusing to enqueue the archiver's own state")
            continue
        if not name or name in (".", ".."):
            report.item(Outcome.REFUSED, name or raw_path,
                        reason="a path must name an entry")
            continue
        target = config.drop_dir / name
        if os.path.lexists(target):
            report.item(Outcome.REFUSED, name,
                        reason=f"name collision: {target} already exists in the spool")
            continue
        try:
            os.rename(source, target)
        except OSError as error:
            if error.errno == errno.EXDEV:
                reason = ("cross-volume: the spool is on a different filesystem; "
                          "move the item there yourself")
            else:
                reason = error.strerror or str(error)
            report.item(Outcome.REFUSED, name, reason=reason)
            continue
        report.item(Outcome.QUEUED, name, archive_path=str(target))
    return emit(context, report)
