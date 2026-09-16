"""`dropin recover --into STATE_DIR`."""

from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import sys

from ..config import ConfigError, _check_password_file
from ..engine.interface import EngineError
from ..pipeline.writer_lock import LockHeld
from ..recover import RecoverRefused, recover
from ..report import EXIT_RUN_REFUSED, EXIT_USAGE, Report
from ..store import records
from . import Context, emit, tools_gate


def run(context, args) -> int:
    into = Path(os.path.realpath(args.into))
    overrides = {"state_dir": into}
    if args.repo:
        overrides["repo"] = args.repo
    if args.password_file:
        try:
            overrides["password_file"] = _check_password_file(args.password_file)
        except ConfigError as error:
            print(f"dropin recover: {error}", file=sys.stderr)
            return EXIT_USAGE
    config = replace(context.config, **overrides)
    for name in ("", "cache", "tmp"):
        (into / name).mkdir(parents=True, exist_ok=True)
    target = Context(config=config, json_output=context.json_output)

    report = Report(verb="recover", run_id=records.new_run_id())
    problem = tools_gate(target)
    if problem:
        report.run_refusal = problem
        return emit(context, report)
    # Under the engine fake the repository is process memory; recovering from
    # it is only meaningful inside a test, and then it is the same engine.
    engine = context.engine if context.fake_engine else target.engine
    try:
        recover(engine, into, report,
                trust_later_exports=args.trust_later_exports)
    except (RecoverRefused, LockHeld, records.IdentityCollision,
            EngineError) as error:
        report.run_refusal = str(error)
        return emit(context, report)
    return emit(context, report)
