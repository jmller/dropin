"""Explicit stale-lock removal; the sole caller of Engine.unlock."""
from __future__ import annotations

from ..engine.interface import EngineError
from ..report import Outcome, Report
from ..store import records
from . import emit, tools_gate


def run(context, _args) -> int:
    report = Report("unlock", records.new_run_id())
    problem = tools_gate(context)
    if problem:
        report.run_refusal = problem
        return emit(context, report)
    try:
        result = context.engine.unlock()
    except EngineError as error:
        report.run_refusal = str(error)
    else:
        report.item(Outcome.INFO, "repository",
                    reason=result or "repository lock check completed")
    return emit(context, report)
