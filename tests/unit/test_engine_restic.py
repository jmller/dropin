"""The restic adapter: argv, strict tags, JSON and exit-code mapping.

Every restic invocation is patched; nothing here runs a binary.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from dropin.config import load
from dropin.engine.interface import EngineError, Identity, TagError, parse_tags
from dropin.engine.restic import ResticEngine

STORE = "0123456789abcdef0123456789abcdef"
OCC = f"{STORE}.01J0000000000000000000000A"
ATTEMPT = f"{STORE}.01J0000000000000000000000B"
DIGEST = "c" * 64

CONFIG = """
[paths]
drop_dir  = "{drop}"
state_dir = "{state}"
[repository]
repo          = "rclone:archive:/dropin"
password_file = "{password}"
[tools]
restic = "/usr/local/bin/restic"
rclone = "/usr/local/bin/rclone"
restic_min = "0.19.1"
rclone_min = "1.75.1"
rclone_connections = 2
timeout_seconds = 3600
pack_size_mb = 16
cache_max_mb = 2048
[drain]
settle_seconds = 5
sample_gap_seconds = 2
max_attempts = 3
retry_backoff_seconds = 300
[ownership]
lsof = "lsof"
[launchd]
label = "dev.dropin.drain"
interval = 900
"""


def canonical_tags(**overrides):
    values = dict(v="1", store=STORE, occ=OCC, attempt=ATTEMPT, seq="1",
                  kind="dir")
    values["catalog-sha256"] = DIGEST
    values.update({k.replace("_", "-"): v for k, v in overrides.items()})
    return [f"dropin:{key}={value}" for key, value in values.items()]


class TagGrammarTest(unittest.TestCase):
    """One value per reserved key, canonical formats, no normalisation."""

    def test_canonical_tag_set_parses(self):
        identity = parse_tags(canonical_tags())
        self.assertIsInstance(identity, Identity)
        self.assertEqual(identity.store_id, STORE)
        self.assertEqual(identity.occ_id, OCC)
        self.assertEqual(identity.attempt_id, ATTEMPT)
        self.assertEqual(identity.export_seq, 1)
        self.assertEqual(identity.kind, "dir")
        self.assertEqual(identity.catalog_sha256, DIGEST)

    def test_sequence_boundaries(self):
        self.assertEqual(parse_tags(canonical_tags(seq="1")).export_seq, 1)
        top = str(2 ** 63 - 2)
        self.assertEqual(parse_tags(canonical_tags(seq=top)).export_seq, 2 ** 63 - 2)

    def test_non_reserved_tags_are_ignored_but_allowed(self):
        identity = parse_tags(canonical_tags() + ["someone-elses-tag"])
        self.assertEqual(identity.occ_id, OCC)

    def test_missing_reserved_key_rejected(self):
        for index in range(7):
            candidate = canonical_tags()
            removed = candidate.pop(index)
            with self.subTest(missing=removed):
                with self.assertRaises(TagError):
                    parse_tags(candidate)

    def test_duplicate_reserved_key_rejected(self):
        with self.assertRaises(TagError) as caught:
            parse_tags(canonical_tags() + ["dropin:seq=2"])
        self.assertIn("seq", str(caught.exception))

    def test_duplicate_reserved_key_with_identical_value_also_rejected(self):
        with self.assertRaises(TagError):
            parse_tags(canonical_tags() + ["dropin:seq=1"])

    def test_sequence_rejects_signs_leading_zeroes_and_range(self):
        for value in ("+1", "-1", "01", "0", "1 ", " 1", "1_000", "",
                      str(2 ** 63 - 1), str(2 ** 63), "0x1"):
            with self.subTest(seq=value):
                with self.assertRaises(TagError):
                    parse_tags(canonical_tags(seq=value))

    def test_version_must_be_exactly_one(self):
        for value in ("2", "1.0", "01", "v1", ""):
            with self.subTest(v=value):
                with self.assertRaises(TagError):
                    parse_tags(canonical_tags(v=value))

    def test_kind_must_be_one_of_three(self):
        for value in ("File", "files", "", "dir "):
            with self.subTest(kind=value):
                with self.assertRaises(TagError):
                    parse_tags(canonical_tags(kind=value))
        for value in ("file", "dir", "bundle"):
            with self.subTest(kind=value):
                self.assertEqual(parse_tags(canonical_tags(kind=value)).kind, value)

    def test_store_must_be_32_lowercase_hex(self):
        for value in (STORE.upper(), STORE[:-1], STORE + "a", "g" * 32, ""):
            with self.subTest(store=value):
                with self.assertRaises(TagError):
                    parse_tags(canonical_tags(store=value))

    def test_digest_must_be_64_lowercase_hex(self):
        for value in (DIGEST.upper(), DIGEST[:-1], "z" * 64, ""):
            with self.subTest(digest=value):
                with self.assertRaises(TagError):
                    parse_tags(canonical_tags(**{"catalog_sha256": value}))

    def test_identifier_grammar(self):
        bad = [
            f"{STORE}.01J0000000000000000000000",       # 25 chars
            f"{STORE}.01J0000000000000000000000AA",     # 27 chars
            f"{STORE}.01j0000000000000000000000a",      # lowercase ULID
            f"{STORE}.01I0000000000000000000000A",      # excluded letter I
            f"{STORE}.01L0000000000000000000000A",      # excluded letter L
            f"{STORE}.01O0000000000000000000000A",      # excluded letter O
            f"{STORE}.01U0000000000000000000000A",      # excluded letter U
            f"{STORE.upper()}.01J0000000000000000000000A",
            f"{STORE}-01J0000000000000000000000A",      # wrong separator
            "01J0000000000000000000000A",               # unnamespaced
        ]
        for value in bad:
            with self.subTest(occ=value):
                with self.assertRaises(TagError):
                    parse_tags(canonical_tags(occ=value))

    def test_identifier_namespace_must_equal_the_store_tag(self):
        other = "f" * 32
        with self.assertRaises(TagError):
            parse_tags(canonical_tags(occ=f"{other}.01J0000000000000000000000A"))
        with self.assertRaises(TagError):
            parse_tags(canonical_tags(attempt=f"{other}.01J0000000000000000000000B"))

    def test_traversal_capable_values_are_rejected_not_normalised(self):
        for value in ("../etc", "a/b", "a\\b", "%2e%2e", "a\x00b", "a\nb",
                      "a b", "./a"):
            with self.subTest(value=value):
                with self.assertRaises(TagError):
                    parse_tags(canonical_tags(occ=value))

    def test_tag_without_an_equals_sign_is_not_a_reserved_value(self):
        with self.assertRaises(TagError):
            parse_tags([t for t in canonical_tags() if not t.startswith("dropin:seq")]
                       + ["dropin:seq"])

    def test_identity_round_trips_to_a_canonical_tag_set(self):
        identity = parse_tags(canonical_tags())
        self.assertEqual(sorted(identity.to_tags()), sorted(canonical_tags()))
        self.assertEqual(parse_tags(identity.to_tags()), identity)


class ResticAdapterTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dropin-restic-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        drop, state = root / "drop", root / "state"
        drop.mkdir()
        state.mkdir()
        password = root / "pw"
        password.write_text("x")
        password.chmod(0o600)
        config_path = root / "config.toml"
        config_path.write_text(CONFIG.format(drop=drop, state=state,
                                             password=password))
        self.config = load(config_path)
        self.engine = ResticEngine(self.config)

    def patched_run(self, stdout=b"", stderr=b"", returncode=0):
        completed = subprocess.CompletedProcess([], returncode, stdout, stderr)
        return mock.patch("subprocess.run", return_value=completed)


class ArgvTest(ResticAdapterTestCase):
    def test_backup_argv_matches_the_repository_contract(self):
        summary = json.dumps({"message_type": "summary",
                              "snapshot_id": "d" * 64}).encode()
        with self.patched_run(stdout=summary) as run:
            self.engine.backup(("/drop/item", "/state/export/e.sqlite"),
                               canonical_tags())
        argv = run.call_args.args[0]
        self.assertEqual(argv[0], "/usr/local/bin/restic")
        self.assertIn("--repo", argv)
        self.assertEqual(argv[argv.index("--repo") + 1], "rclone:archive:/dropin")
        self.assertEqual(argv[argv.index("--cache-dir") + 1],
                         str(self.config.cache_dir))
        self.assertIn("--option", argv)
        self.assertIn("rclone.program=/usr/local/bin/rclone", argv)
        self.assertIn("rclone.connections=2", argv)
        self.assertEqual(argv[argv.index("--pack-size") + 1], "16")
        self.assertIn("--json", argv)
        self.assertIn("backup", argv)
        for tag in canonical_tags():
            self.assertIn(tag, argv)
        self.assertEqual(argv.count("--tag"), 7)
        self.assertEqual(argv[-2:], ["/drop/item", "/state/export/e.sqlite"])

    def test_environment_is_the_configured_one(self):
        summary = json.dumps({"message_type": "summary",
                              "snapshot_id": "d" * 64}).encode()
        with self.patched_run(stdout=summary) as run:
            self.engine.backup(("/drop/item",), canonical_tags())
        env = run.call_args.kwargs["env"]
        self.assertEqual(env["RESTIC_PASSWORD_FILE"],
                         str(self.config.password_file))
        self.assertEqual(env["RESTIC_CACHE_DIR"], str(self.config.cache_dir))
        self.assertEqual(env["TMPDIR"], str(self.config.tmp_dir))
        self.assertNotIn("RESTIC_REPOSITORY", env)

    def test_timeout_comes_from_config(self):
        with self.patched_run(stdout=b"[]") as run:
            self.engine.snapshots()
        self.assertEqual(run.call_args.kwargs["timeout"], 3600)

    def test_snapshots_filters_on_one_tag(self):
        with self.patched_run(stdout=b"[]") as run:
            self.engine.snapshots(tag=f"dropin:attempt={ATTEMPT}")
        argv = run.call_args.args[0]
        self.assertEqual(argv[argv.index("--tag") + 1], f"dropin:attempt={ATTEMPT}")

    def test_check_uses_an_explicit_data_subset_and_the_common_process_contract(self):
        with self.patched_run() as run:
            self.engine.check("2/3")
        argv = run.call_args.args[0]
        self.assertEqual(argv[-3:], ["check", "--read-data-subset", "2/3"])
        self.assertEqual(run.call_args.kwargs["env"]["RESTIC_PASSWORD_FILE"],
                         str(self.config.password_file))
        self.assertEqual(run.call_args.kwargs["timeout"], 3600)

    def test_unlock_uses_only_the_unlock_operation_and_returns_backend_output(self):
        with self.patched_run(stdout=b"successfully removed 2 locks\n") as run:
            result = self.engine.unlock()
        self.assertEqual(run.call_args.args[0][-1:], ["unlock"])
        self.assertNotIn("snapshots", run.call_args.args[0])
        self.assertEqual(result, "successfully removed 2 locks")


class JsonParsingTest(ResticAdapterTestCase):
    def test_summary_event_supplies_the_snapshot_id(self):
        events = b"\n".join([
            json.dumps({"message_type": "status", "percent_done": 0.5}).encode(),
            json.dumps({"message_type": "summary", "snapshot_id": "e" * 64}).encode(),
        ])
        with self.patched_run(stdout=events):
            result = self.engine.backup(("/drop/item",), canonical_tags())
        self.assertEqual(result.snapshot_id, "e" * 64)
        self.assertEqual(result.exit_code, 0)

    def test_backup_without_a_summary_event_is_a_tool_error(self):
        with self.patched_run(stdout=b'{"message_type":"status"}'):
            with self.assertRaises(EngineError) as caught:
                self.engine.backup(("/drop/item",), canonical_tags())
        self.assertEqual(caught.exception.kind, "tool-error")

    def test_snapshots_are_sorted_by_time_by_us(self):
        payload = json.dumps([
            {"id": "b" * 64, "time": "2026-09-06T12:00:00Z", "paths": ["/b"],
             "tags": canonical_tags(seq="2")},
            {"id": "a" * 64, "time": "2026-09-06T11:00:00Z", "paths": ["/a"],
             "tags": canonical_tags(seq="1")},
        ]).encode()
        with self.patched_run(stdout=payload):
            snapshots = self.engine.snapshots()
        self.assertEqual([s.id for s in snapshots], ["a" * 64, "b" * 64])

    def test_ls_parses_nodes_and_skips_the_header(self):
        lines = b"\n".join([
            json.dumps({"struct_type": "snapshot", "id": "a" * 64}).encode(),
            json.dumps({"struct_type": "node", "path": "/drop/item",
                        "type": "dir"}).encode(),
            json.dumps({"struct_type": "node", "path": "/drop/item/f.txt",
                        "type": "file", "size": 7}).encode(),
        ])
        with self.patched_run(stdout=lines):
            nodes = list(self.engine.ls("a" * 64, "/drop/item"))
        self.assertEqual([n.path for n in nodes],
                         ["/drop/item", "/drop/item/f.txt"])
        self.assertEqual(nodes[1].size, 7)
        self.assertIsNone(nodes[0].size)

    def test_ls_passes_the_item_path_so_ancestors_are_excluded(self):
        # Without the path argument restic also lists every ancestor dir.
        with self.patched_run(stdout=b"") as run:
            list(self.engine.ls("a" * 64, "/drop/item"))
        argv = run.call_args.args[0]
        self.assertEqual(argv[-2:], ["a" * 64, "/drop/item"])
        self.assertIn("--recursive", argv)

    def test_export_path_is_located_by_suffix_never_by_index(self):
        paths = ["/state/export/other.sqlite", "/drop/item",
                 f"/state/export/{OCC}-{ATTEMPT}.sqlite"]
        self.assertEqual(self.engine.locate_export(paths, OCC, ATTEMPT),
                         f"/state/export/{OCC}-{ATTEMPT}.sqlite")
        with self.assertRaises(EngineError):
            self.engine.locate_export(["/drop/item"], OCC, ATTEMPT)


class ExitCodeMappingTest(ResticAdapterTestCase):
    def test_documented_exit_codes_map_to_kinds(self):
        cases = {3: "partial", 10: "no-repo", 11: "locked", 12: "bad-password",
                 1: "tool-error", 130: "tool-error"}
        for code, kind in cases.items():
            with self.subTest(code=code):
                with self.patched_run(stdout=b"[]", stderr=b"boom", returncode=code):
                    if code == 3:
                        continue  # exit 3 is a backup outcome, tested below
                    with self.assertRaises(EngineError) as caught:
                        self.engine.snapshots()
                    self.assertEqual(caught.exception.kind, kind)

    def test_exit_three_is_a_partial_backup_not_an_exception(self):
        summary = json.dumps({"message_type": "summary",
                              "snapshot_id": "f" * 64}).encode()
        with self.patched_run(stdout=summary, returncode=3):
            result = self.engine.backup(("/drop/item",), canonical_tags())
        self.assertEqual(result.exit_code, 3)
        self.assertEqual(result.snapshot_id, "f" * 64)

    def test_check_failure_is_corruption_evidence_not_a_structure_success(self):
        with self.patched_run(stderr=b"data blob damaged", returncode=1):
            with self.assertRaises(EngineError) as caught:
                self.engine.check("1/1")
        self.assertEqual(caught.exception.kind, "corrupt")
        self.assertIn("data blob damaged", caught.exception.stderr_tail)

    def test_stderr_tail_is_attached_to_failures(self):
        stderr = ("\n".join(f"line {index}" for index in range(50))).encode()
        with self.patched_run(stderr=stderr, returncode=1):
            with self.assertRaises(EngineError) as caught:
                self.engine.snapshots()
        tail = caught.exception.stderr_tail.splitlines()
        self.assertEqual(len(tail), 20)
        self.assertEqual(tail[-1], "line 49")

    def test_timeout_is_a_tool_error(self):
        with mock.patch("subprocess.run",
                        side_effect=subprocess.TimeoutExpired([], 1)):
            with self.assertRaises(EngineError) as caught:
                self.engine.snapshots()
        self.assertEqual(caught.exception.kind, "tool-error")

    def test_missing_binary_is_a_tool_error(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError()):
            with self.assertRaises(EngineError) as caught:
                self.engine.snapshots()
        self.assertEqual(caught.exception.kind, "tool-error")


class DumpStreamTest(ResticAdapterTestCase):
    def popen(self, chunks: list[bytes], returncode: int = 0, stderr: bytes = b""):
        process = mock.MagicMock()
        process.stdout = __import__("io").BytesIO(b"".join(chunks))
        process.stderr = __import__("io").BytesIO(stderr)
        process.wait.return_value = returncode
        process.returncode = returncode
        return mock.patch("subprocess.Popen", return_value=process)

    def test_dump_consumes_to_eof_then_waits(self):
        with self.popen([b"payload"]) as popen:
            with self.engine.dump("a" * 64, "/drop/item/f.txt") as stream:
                self.assertEqual(stream.read(), b"payload")
        popen.return_value.wait.assert_called()

    def test_nonzero_exit_after_the_stream_is_a_tool_error_without_integrity_evidence(self):
        with self.popen([b"partial"], returncode=1, stderr=b"ciphertext verification failed"):
            with self.assertRaises(EngineError) as caught:
                with self.engine.dump("a" * 64, "/drop/item/f.txt") as stream:
                    stream.read()
        self.assertEqual(caught.exception.kind, "tool-error")

    def test_unknown_exit_does_not_erase_consumer_integrity_evidence(self):
        integrity_error = ValueError("verified content mismatch")
        with self.popen([b"partial"], returncode=1, stderr=b"read failed"):
            with self.assertRaises(ValueError) as caught:
                with self.engine.dump("a" * 64, "/drop/item/f.txt"):
                    raise integrity_error
        self.assertIs(caught.exception, integrity_error)

    def test_operational_exit_overrides_consumer_short_stream_error(self):
        with self.popen([b"partial"], returncode=10, stderr=b"repository missing"):
            with self.assertRaises(EngineError) as caught:
                with self.engine.dump("a" * 64, "/drop/item/f.txt"):
                    raise ValueError("short stream")
        self.assertEqual(caught.exception.kind, "no-repo")

    def test_dump_closes_both_pipes_after_full_or_partial_consumption(self):
        for read_size in (-1, 1, 0):
            with self.subTest(read_size=read_size):
                with self.popen([b"payload"]) as popen:
                    process = popen.return_value
                    with self.engine.dump("a" * 64, "/drop/item") as stream:
                        stream.read(read_size)
                        self.assertFalse(stream.closed)
                    self.assertTrue(process.stdout.closed)
                    self.assertTrue(process.stderr.closed)
                    process.wait.assert_called_once_with(
                        timeout=self.engine.config.timeout_seconds)

    def test_dump_closes_both_pipes_when_consumer_raises(self):
        failure = ValueError("consumer rejected the payload")
        with self.popen([b"payload"]) as popen:
            with self.assertRaises(ValueError) as caught:
                with self.engine.dump("a" * 64, "/drop/item") as stream:
                    stream.read(1)
                    raise failure
            self.assertIs(caught.exception, failure)
            self.assertTrue(popen.return_value.stdout.closed)
            self.assertTrue(popen.return_value.stderr.closed)
            popen.return_value.wait.assert_called_once()

    def test_dump_closes_both_pipes_on_nonzero_exit(self):
        with self.popen([b"partial"], returncode=1, stderr=b"bad ciphertext") as popen:
            with self.assertRaises(EngineError) as caught:
                with self.engine.dump("a" * 64, "/drop/item") as stream:
                    stream.read()
            self.assertEqual(caught.exception.kind, "tool-error")
            self.assertEqual(caught.exception.stderr_tail, "bad ciphertext")
            self.assertIn("restic dump exited 1", caught.exception.message)
            self.assertTrue(popen.return_value.stdout.closed)
            self.assertTrue(popen.return_value.stderr.closed)

    def test_dump_documented_exit_codes_keep_operational_categories(self):
        cases = {10: "no-repo", 11: "locked", 12: "bad-password", 99: "tool-error"}
        for code, kind in cases.items():
            with self.subTest(code=code), self.popen(
                    [b"partial"], returncode=code, stderr=b"restic diagnostic"):
                with self.assertRaises(EngineError) as caught:
                    with self.engine.dump("a" * 64, "/drop/item") as stream:
                        stream.read()
                self.assertEqual(caught.exception.kind, kind)
                self.assertEqual(caught.exception.stderr_tail, "restic diagnostic")
                self.assertIn(f"restic dump exited {code}", caught.exception.message)

    def test_dump_incompatible_repository_diagnostic_overrides_exit_code(self):
        stderr = b"repository version is too new for this restic"
        with self.popen([b""], returncode=1, stderr=stderr):
            with self.assertRaises(EngineError) as caught:
                with self.engine.dump("a" * 64, "/drop/item") as stream:
                    stream.read()
        self.assertEqual(caught.exception.kind, "incompatible-repository")

    def test_dump_closes_both_pipes_when_stderr_read_fails(self):
        with self.popen([b"payload"]) as popen:
            process = popen.return_value
            with mock.patch.object(process.stderr, "read",
                                   side_effect=OSError("pipe read failed")):
                with self.assertRaisesRegex(OSError, "pipe read failed"):
                    with self.engine.dump("a" * 64, "/drop/item") as stream:
                        stream.read()
            self.assertTrue(process.stdout.closed)
            self.assertTrue(process.stderr.closed)

    def test_dump_timeout_is_a_tool_error_and_closes_both_pipes(self):
        with self.popen([b"payload"]) as popen:
            process = popen.return_value
            process.wait.side_effect = subprocess.TimeoutExpired([], 1)
            with self.assertRaises(EngineError) as caught:
                with self.engine.dump("a" * 64, "/drop/item") as stream:
                    stream.read()
            self.assertEqual(caught.exception.kind, "tool-error")
            self.assertIn("restic dump timed out", caught.exception.message)
            self.assertTrue(process.stdout.closed)
            self.assertTrue(process.stderr.closed)

    def test_tar_archive_flag_is_passed(self):
        with self.popen([b""]) as popen:
            with self.engine.dump("a" * 64, "/drop/item", archive="tar") as stream:
                stream.read()
        argv = popen.call_args.args[0]
        self.assertEqual(argv[argv.index("--archive") + 1], "tar")
