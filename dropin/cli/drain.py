"""`dropin drain`: the driver, plus `--dry-run`."""

from __future__ import annotations

import os
from pathlib import Path
import sys

from ..pipeline.drain import DrainOptions, drain
from ..progress import ProgressEvent, TerminalProgress
from ..report import Outcome, Report
from ..spool.scan import scan
from ..store import records
from . import emit, tools_gate


def run(context, args) -> int:
    report = Report(verb="drain", run_id=records.new_run_id())
    if args.adopt_lineage:
        print("dropin drain: --adopt-lineage is not implemented yet; use "
              "`dropin recover --into` to merge a foreign lineage", file=sys.stderr)
        return 2

    active = TerminalProgress.supported(
        sys.stderr, json_output=context.json_output, dry_run=args.dry_run)
    progress = TerminalProgress(
        stream=sys.stderr, color="NO_COLOR" not in os.environ) if active else None
    if progress is None:
        _execute(context, args, report, None)
    else:
        with progress:
            progress.update(ProgressEvent("tools", "Checking archive tools"))
            _execute(context, args, report, progress.update)
    return emit(context, report)


def _execute(context, args, report: Report, progress) -> None:
    problem = tools_gate(context)
    if problem:
        report.run_refusal = problem
        try:
            paths = scan(context.config.drop_dir)
        except OSError as error:
            report.run_refusal += f"; spool scan failed: {error}"
        else:
            for path in paths:
                report.item(Outcome.REFUSED, path.name, reason=problem)
        return

    if args.dry_run:
        _dry_run(context, report, args)
        return

    context.db  # a missing store is a usage error, raised before any work
    options = DrainOptions(retry_exhausted=args.retry_exhausted,
                           settle_seconds=args.settle, progress=progress)
    # The dispatcher holds the writer lock for the whole verb.
    drain(context, report, options, lock=False)


def _dry_run(context, report: Report, args) -> None:
    """Quiescence, preflight, and capture only; nothing is written anywhere."""
    from ..capture.extract import CaptureAborted, capture_item
    from ..spool.admission import admit
    from ..spool.scan import scan
    from ..spool.walk import SpecialEntry, walk

    config = context.config
    settle = config.settle_seconds if args.settle is None else args.settle
    for path in scan(config.drop_dir):
        name = Path(path).name
        if not admit(path, settle_seconds=settle,
                     sample_gap_seconds=config.sample_gap_seconds):
            try:
                for _ in walk(path):
                    pass
            except SpecialEntry as error:
                report.item(Outcome.REFUSED, name,
                            reason=f"unsupported entry: {error}")
                continue
            except OSError as error:
                report.item(Outcome.REFUSED, name,
                            reason=f"unreadable: {error.strerror or error}")
                continue
            report.item(Outcome.DEFERRED, name, reason="not quiescent")
            continue
        try:
            item = capture_item(context.macos, path)
        except SpecialEntry as error:
            report.item(Outcome.REFUSED, name, reason=f"unsupported entry: {error}")
        except CaptureAborted:
            report.item(Outcome.DEFERRED, name, reason="source changed during capture")
        except OSError as error:
            report.item(Outcome.REFUSED, name,
                        reason=f"unreadable: {error.strerror or error}")
        else:
            report.item(Outcome.INFO, name, kind=item.kind, sha256=item.root_sha256,
                        size=item.size_bytes,
                        reason=f"dry-run: would publish {item.kind} with "
                               f"{item.entry_count} entries, {item.size_bytes} bytes")
