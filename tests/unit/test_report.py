"""Outcome records, renderers, and the exit-code policy."""

import json
import unittest

from dropin.report import (EXIT_ITEM_FAILURE, EXIT_OK, EXIT_RUN_REFUSED,
                           EXIT_USAGE, EXIT_VERIFICATION, Outcome, Report,
                           OutcomeRecord, exit_code_for)


def record(**overrides):
    fields = dict(verb="drain", outcome=Outcome.ARCHIVED,
                  archive_path="01J.../report.pdf", name="report.pdf",
                  kind="file", state="evicted", snapshot="8e039f95",
                  sha256="a" * 64, size=12345, dedup=False, reason=None,
                  run_id="run-1")
    fields.update(overrides)
    return OutcomeRecord(**fields)


class NdjsonTest(unittest.TestCase):
    def test_documented_field_set(self):
        payload = json.loads(record().to_json())
        self.assertEqual(set(payload), {
            "verb", "outcome", "archive_path", "name", "kind", "state",
            "snapshot", "sha256", "size", "dedup", "reason", "run_id"})
        self.assertEqual(payload["outcome"], "archived")
        self.assertIsNone(payload["reason"])
        self.assertFalse(payload["dedup"])

    def test_one_object_per_line(self):
        self.assertNotIn("\n", record().to_json())

    def test_retained_is_a_documented_outcome(self):
        payload = json.loads(record(outcome=Outcome.RETAINED,
                                    reason="open writer: pid 42").to_json())
        self.assertEqual(payload["outcome"], "retained")
        self.assertEqual(payload["reason"], "open writer: pid 42")

    def test_every_contract_outcome_exists(self):
        self.assertEqual(
            {o.value for o in Outcome},
            {"archived", "already-archived", "deferred", "refused", "retained",
             "verified", "corrupt", "missing", "restored", "orphaned", "queued",
             "info"})

    def test_non_utf8_name_is_escaped_and_carries_name_b64(self):
        raw = b"caf\xe9.txt"
        payload = json.loads(record(name=raw.decode("utf-8", "surrogateescape"),
                                    archive_path="01J.../x").to_json())
        self.assertIn("name_b64", payload)
        import base64

        self.assertEqual(base64.b64decode(payload["name_b64"]), raw)
        json.dumps(payload)  # round-trips as valid JSON

    def test_plain_name_has_no_name_b64(self):
        self.assertNotIn("name_b64", json.loads(record().to_json()))


class HumanLineTest(unittest.TestCase):
    def test_success_line_shows_archive_path(self):
        self.assertEqual(record().to_human(),
                         "archived\treport.pdf\t01J.../report.pdf")

    def test_failure_line_shows_reason_instead(self):
        line = record(outcome=Outcome.REFUSED, reason="special entry: pipe").to_human()
        self.assertEqual(line, "refused\treport.pdf\tspecial entry: pipe")


class ExitCodeTest(unittest.TestCase):
    def test_nothing_to_do_is_zero(self):
        self.assertEqual(exit_code_for([]), EXIT_OK)

    def test_all_succeeded_is_zero(self):
        self.assertEqual(exit_code_for([record(), record(outcome=Outcome.VERIFIED)]),
                         EXIT_OK)

    def test_refused_or_deferred_with_error_is_one(self):
        for outcome in (Outcome.REFUSED, Outcome.RETAINED):
            with self.subTest(outcome=outcome):
                self.assertEqual(exit_code_for([record(), record(outcome=outcome,
                                                                reason="x")]),
                                 EXIT_ITEM_FAILURE)

    def test_corrupt_and_missing_are_four_not_one(self):
        # The exit-code table and the `verify` verb must agree.
        for outcome in (Outcome.CORRUPT, Outcome.MISSING):
            with self.subTest(outcome=outcome):
                self.assertEqual(exit_code_for([record(outcome=outcome,
                                                       reason="x")]),
                                 EXIT_VERIFICATION)

    def test_verification_outranks_item_failure(self):
        self.assertEqual(
            exit_code_for([record(outcome=Outcome.REFUSED, reason="x"),
                           record(outcome=Outcome.CORRUPT, reason="y")]),
            EXIT_VERIFICATION)

    def test_run_level_refusal_is_three(self):
        for reason in ("stale store", "lineage mismatch", "writer lock held",
                       "repository unreachable", "tools too old"):
            with self.subTest(reason=reason):
                self.assertEqual(exit_code_for([], run_refusal=reason),
                                 EXIT_RUN_REFUSED)

    def test_run_level_refusal_outranks_item_outcomes(self):
        self.assertEqual(exit_code_for([record(outcome=Outcome.CORRUPT, reason="x")],
                                       run_refusal="stale store"),
                         EXIT_RUN_REFUSED)

    def test_usage_error_is_two(self):
        self.assertEqual(EXIT_USAGE, 2)

    def test_deferred_without_error_is_still_one(self):
        # A deferred item did not archive; the run must not claim success.
        self.assertEqual(exit_code_for([record(outcome=Outcome.DEFERRED,
                                               reason="source changed")]),
                         EXIT_ITEM_FAILURE)

    def test_informational_outcomes_do_not_fail_a_run(self):
        for outcome in (Outcome.QUEUED, Outcome.INFO, Outcome.ALREADY_ARCHIVED,
                        Outcome.RESTORED, Outcome.ORPHANED):
            with self.subTest(outcome=outcome):
                self.assertEqual(exit_code_for([record(outcome=outcome)]), EXIT_OK)


class StatusExitPolicyTest(unittest.TestCase):
    """`status` is observational: 0 healthy, 1 attention, 3 unreachable."""

    def test_status_uses_its_own_policy(self):
        from dropin.report import status_exit_code

        self.assertEqual(status_exit_code(attention=False, unreachable=False), 0)
        self.assertEqual(status_exit_code(attention=True, unreachable=False), 1)
        self.assertEqual(status_exit_code(attention=True, unreachable=True), 3)

    def test_offline_status_never_reports_unreachable(self):
        from dropin.report import status_exit_code

        self.assertEqual(
            status_exit_code(attention=False, unreachable=True, offline=True), 0)


class ReportTest(unittest.TestCase):
    def test_report_collects_and_renders(self):
        report = Report(verb="drain", run_id="run-1")
        report.add(record())
        report.add(record(outcome=Outcome.REFUSED, name="bad", reason="why"))
        self.assertEqual(report.exit_code(), EXIT_ITEM_FAILURE)
        self.assertEqual(len(report.records), 2)
        lines = report.render_json().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual([json.loads(line)["outcome"] for line in lines],
                         ["archived", "refused"])

    def test_report_counts_by_outcome(self):
        report = Report(verb="drain", run_id="run-1")
        report.add(record())
        report.add(record())
        report.add(record(outcome=Outcome.REFUSED, reason="x"))
        self.assertEqual(report.counts()[Outcome.ARCHIVED], 2)
        self.assertEqual(report.counts()[Outcome.REFUSED], 1)
