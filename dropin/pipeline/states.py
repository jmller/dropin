"""Occurrence states and the transitions the protocol permits.

The pipeline order is linear; `abandoned` sits outside it as the terminal state
of a source that changed. Two rules carry most of the safety weight:

* `evicted` is reachable only from `evicting`, so nothing is called deleted
  without a journalled intent;
* `evicting` never reaches `abandoned`, so a partially deleted tree is never
  re-captured as a fresh occurrence.
"""

from __future__ import annotations

from enum import Enum


class State(str, Enum):
    RECORDED = "recorded"
    TRANSFERRED = "transferred"
    VERIFIED = "verified"
    RECOVERABLE = "recoverable"
    EVICTING = "evicting"
    EVICTED = "evicted"
    ABANDONED = "abandoned"


#: The linear pipeline. `abandoned` is deliberately absent: it is terminal, not
#: a rank, and comparing it to a pipeline state is a bug worth raising on.
ORDER: tuple[State, ...] = (
    State.RECORDED, State.TRANSFERRED, State.VERIFIED, State.RECOVERABLE,
    State.EVICTING, State.EVICTED,
)

ALLOWED: dict[State, frozenset[State]] = {
    # A non-source attempt failure regresses to `recorded`; a source change
    # before the eviction intent abandons.
    State.RECORDED: frozenset({State.TRANSFERRED, State.ABANDONED}),
    State.TRANSFERRED: frozenset({State.VERIFIED, State.RECORDED,
                                  State.ABANDONED}),
    State.VERIFIED: frozenset({State.RECOVERABLE, State.RECORDED,
                               State.ABANDONED}),
    # Publication is confirmed by now, so there is no way back to `recorded`;
    # gate (d) abandons the occurrence while the attempt stays confirmed.
    State.RECOVERABLE: frozenset({State.EVICTING, State.ABANDONED}),
    # A refused deletion pass stays put and is reported `retained`; staying is
    # not a transition, so `evicting` is only ever entered from `recoverable`.
    State.EVICTING: frozenset({State.EVICTED}),
    State.EVICTED: frozenset(),
    State.ABANDONED: frozenset(),
}


def can_transition(current: State, target: State) -> bool:
    return target in ALLOWED[current]


def is_before(earlier: State, later: State) -> bool:
    """Order two pipeline states. Raises for `abandoned`, which has no rank."""
    try:
        return ORDER.index(earlier) < ORDER.index(later)
    except ValueError as error:
        raise ValueError(
            f"{State.ABANDONED.value} is terminal and has no pipeline rank"
        ) from error
