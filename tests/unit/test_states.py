"""Occurrence state ordering and allowed transitions."""

import unittest

from dropin.pipeline.states import ALLOWED, State, can_transition, is_before


class StateOrderTest(unittest.TestCase):
    def test_pipeline_order(self):
        ordered = [State.RECORDED, State.TRANSFERRED, State.VERIFIED,
                   State.RECOVERABLE, State.EVICTING, State.EVICTED]
        for earlier, later in zip(ordered, ordered[1:]):
            with self.subTest(earlier=earlier, later=later):
                self.assertTrue(is_before(earlier, later))
                self.assertFalse(is_before(later, earlier))

    def test_abandoned_is_outside_the_linear_order(self):
        # It is terminal, not a rank: comparing it to a pipeline state is a bug.
        with self.assertRaises(ValueError):
            is_before(State.ABANDONED, State.EVICTED)


class TransitionTest(unittest.TestCase):
    def test_forward_transitions_allowed(self):
        for earlier, later in ((State.RECORDED, State.TRANSFERRED),
                               (State.TRANSFERRED, State.VERIFIED),
                               (State.VERIFIED, State.RECOVERABLE),
                               (State.RECOVERABLE, State.EVICTING),
                               (State.EVICTING, State.EVICTED)):
            with self.subTest(transition=(earlier, later)):
                self.assertTrue(can_transition(earlier, later))

    def test_attempt_failure_regresses_to_recorded(self):
        for state in (State.TRANSFERRED, State.VERIFIED):
            with self.subTest(state=state):
                self.assertTrue(can_transition(state, State.RECORDED))

    def test_recoverable_never_regresses_to_recorded(self):
        # Publication is confirmed by then; only gate (d) abandonment leaves it.
        self.assertFalse(can_transition(State.RECOVERABLE, State.RECORDED))

    def test_abandoned_reachable_before_the_eviction_intent_only(self):
        for state in (State.RECORDED, State.TRANSFERRED, State.VERIFIED,
                      State.RECOVERABLE):
            with self.subTest(state=state):
                self.assertTrue(can_transition(state, State.ABANDONED))
        self.assertFalse(can_transition(State.EVICTING, State.ABANDONED))
        self.assertFalse(can_transition(State.EVICTED, State.ABANDONED))

    def test_evicted_only_from_evicting(self):
        for state in State:
            if state is State.EVICTING:
                continue
            with self.subTest(state=state):
                self.assertFalse(can_transition(state, State.EVICTED))

    def test_evicting_only_from_recoverable(self):
        for state in State:
            if state is State.RECOVERABLE:
                continue
            with self.subTest(state=state):
                self.assertFalse(can_transition(state, State.EVICTING))

    def test_retention_is_staying_put_not_a_self_transition(self):
        # A refused deletion pass reports `retained` and leaves the row alone.
        # Modelling that as a transition would let `evicting` be entered from
        # somewhere other than `recoverable`.
        self.assertFalse(can_transition(State.EVICTING, State.EVICTING))

    def test_terminal_states_have_no_exits(self):
        for terminal in (State.EVICTED, State.ABANDONED):
            with self.subTest(state=terminal):
                self.assertEqual(ALLOWED[terminal], frozenset())

    def test_no_skipping_a_gate(self):
        for earlier, later in ((State.RECORDED, State.VERIFIED),
                               (State.RECORDED, State.RECOVERABLE),
                               (State.TRANSFERRED, State.RECOVERABLE),
                               (State.VERIFIED, State.EVICTING),
                               (State.RECOVERABLE, State.EVICTED)):
            with self.subTest(transition=(earlier, later)):
                self.assertFalse(can_transition(earlier, later))

    def test_state_values_are_the_stored_strings(self):
        self.assertEqual(
            {s.value for s in State},
            {"recorded", "transferred", "verified", "recoverable", "evicting",
             "evicted", "abandoned"})
