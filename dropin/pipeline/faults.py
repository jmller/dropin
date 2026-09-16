"""Crash injection for the pipeline.

`DROPIN_FAULT_AFTER=<point>` aborts the process at the named point, immediately
after the transition there has been committed. It exists so the resume paths are
exercised by real re-runs rather than by reasoning about them. A separate module
so `evict` can hook its own points without importing the driver.
"""

from __future__ import annotations

import os

FAULT_ENV = "DROPIN_FAULT_AFTER"

#: Every point the pipeline offers, in pipeline order.
POINTS = ("recorded", "attempt-started", "backup-returned", "transferred",
          "verified", "recoverable", "intent-written", "mid-deletion",
          "root-removed")


class FaultInjected(Exception):
    """The configured crash point was reached. Propagates out of the run."""

    def __init__(self, point: str) -> None:
        super().__init__(f"injected fault after {point}")
        self.point = point


def fault_after(point: str) -> None:
    if point not in POINTS:
        raise ValueError(f"unknown fault point {point!r}")
    if os.environ.get(FAULT_ENV) == point:
        raise FaultInjected(point)
