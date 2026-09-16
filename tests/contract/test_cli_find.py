"""Real CLI wire output with tools unreachable and writer lock held."""

import base64
import contextlib
import io
import json
from unittest.mock import patch

from dropin.pipeline.writer_lock import writer_lock
from tests.query_support import QueryTestCase


class CliFindTest(QueryTestCase):
    def test_find_paths_nul_and_json_flags_before_or_after_verb(self):
        path = self.paths["Tax-March.PDF"]
        for flags in (("-0", "find"), ("find", "-0")):
            result = self.cli(*flags, "--name", "TAX-MARCH")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, path.encode() + b"\0")
            self.assertEqual(result.stderr, b"")
        for flags in (("--json", "find"), ("find", "--json")):
            result = self.cli(*flags, "--name", "TAX-MARCH")
            self.assertEqual(result.returncode, 0, result.stderr)
            row = json.loads(result.stdout)
            self.assertEqual(row["archive_path"], path)
            self.assertIn("attributes", row)
            self.assertIn("attribute_values", row)
            self.assertIn("entry", row)
        result = self.cli("find", "--name", "TAX-MARCH")
        self.assertEqual(result.stdout, path.encode() + b"\n")

    def test_name_with_newline_is_unambiguous_with_nul(self):
        self.seed("new\nline.txt")
        result = self.cli("find", "--name", "new", "-0")
        self.assertEqual(result.stdout, self.paths["new\nline.txt"].encode() + b"\0")

    def test_show_human_path_object_hash_array_json_ndjson_and_missing(self):
        path = self.paths["Notes.txt"]
        result = self.cli("show", path)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["archive_path"], path)
        result = self.cli("show", self.hashes["Notes.txt"])
        self.assertEqual(json.loads(result.stdout)[0]["archive_path"], path)
        result = self.cli("--json", "show", path)
        self.assertEqual(len(result.stdout.splitlines()), 1)
        self.assertEqual(json.loads(result.stdout)["archive_path"], path)
        result = self.cli("show", "no/such/path")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, b"")
        self.assertIn(b"no/such/path", result.stderr)
        result = self.cli("--json", "show", "f" * 64)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b"")

    def test_empty_find_is_success_and_invalid_filters_exit_two(self):
        result = self.cli("find", "--name", "no match")
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"", b""))
        for flags in (("--limit", "-1"), ("--since", "bad"), ("--hash", "bad"),
                      ("--name", "a", "--glob", "*"), ("--text", '"bad'),
                      ("--size-min", "-1"), ("--kind", "x", "--uti", "y")):
            with self.subTest(flags=flags):
                result = self.cli("find", *flags)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, b"")
                self.assertNotIn(b"Traceback", result.stderr)
        result = self.cli("ls", "--state", "imaginary")
        self.assertEqual(result.returncode, 2, result.stderr)

    def test_overflowing_timezone_offsets_exit_two(self):
        for offset in ("+00:60", "-00:60", "+01:99", "-01:99", "+24:00"):
            with self.subTest(offset=offset):
                result = self.cli("find", "--since", "2026-03-01T00:00:00" + offset)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, b"")
                self.assertIn(b"invalid date", result.stderr)
                self.assertNotIn(b"Traceback", result.stderr)

    def test_cli_queries_under_live_writer_lock_never_use_engine_gate_or_context_db(self):
        from dropin.__main__ import main
        from dropin.cli import Context
        with writer_lock(self.temp.state_dir / "writer.lock", verb="drain"), \
             patch.object(Context, "engine", new_callable=property, fget=lambda _: self.fail("engine")), \
             patch.object(Context, "db", new_callable=property, fget=lambda _: self.fail("writable db")), \
             patch("dropin.cli.tools_gate", side_effect=AssertionError("tools")), \
             patch("dropin.pipeline.writer_lock.writer_lock", side_effect=AssertionError("writer lock")):
            for args in (("find", "--name", "Notes"), ("show", self.paths["Notes.txt"]), ("ls",)):
                with self.subTest(args=args), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(main(["--config", str(self.config_path), *args]), 0)
            # A real child also runs with another process holding the lock.
            result = self.cli("find", "--name", "Notes")
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_ls_state_and_archive_path_and_operational_exception(self):
        self.seed("pending", confirmed=False, state="recorded")
        result = self.cli("ls", "--state", "recorded")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.decode(), f"recorded\t{self.paths['pending']}\n")
        result = self.cli("--json", "ls")
        rows = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(len(rows), 12)
        self.assertNotIn(self.paths["pending"], {r["archive_path"] for r in rows})

    def test_missing_store_is_configuration_error_and_not_created(self):
        self.db.close()
        self.temp.store_path.unlink()
        result = self.cli("find")
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.temp.store_path.exists())

    def test_unreadable_catalog_format_is_a_clear_local_configuration_error(self):
        self.db.close()
        self.temp.store_path.write_bytes(b"not a SQLite catalog")
        result = self.cli("find")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, b"")
        self.assertIn(b"local store", result.stderr)
        self.assertNotIn(b"Traceback", result.stderr)

    def test_non_utf8_record_json_escapes_and_carries_original_name_bytes(self):
        from dropin.cli.find import run
        from dropin.cli import Context
        from dropin.config import load
        from dropin.__main__ import build_parser
        name = "bad\udcff.txt"
        with patch("dropin.cli.find.find", return_value=iter([{"name": name, "archive_path": "id/" + name}])), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            context = Context(load(self.config_path), json_output=True)
            self.assertEqual(run(context, build_parser().parse_args(["find"])), 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["name"], name)
        self.assertEqual(base64.b64decode(payload["name_b64"]), b"bad\xff.txt")
