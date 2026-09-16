"""Dispatcher, stream flags, exits, find-to-get composition."""
import contextlib
import io
import json
from pathlib import Path
from unittest.mock import patch

from tests.retrieve_support import RetrieveTestCase
from tests.support import run_cli


class CliGetTest(RetrieveTestCase):
    def invoke(self, *args, stdin=b''):
        from dropin.__main__ import main
        from dropin.cli import Context
        stdout, stderr = io.BytesIO(), io.StringIO()
        wrapper = io.TextIOWrapper(stdout, encoding='utf-8')
        input_wrapper = io.TextIOWrapper(io.BytesIO(stdin), encoding='utf-8')
        try:
            with patch.object(Context, 'engine', property(lambda _: self.engine)), patch.object(Context, 'db', property(lambda _: self.fail('writable store'))), patch('dropin.cli.tools_gate', return_value=None), patch('dropin.pipeline.writer_lock.writer_lock', side_effect=AssertionError('writer lock')), patch('sys.stdin', input_wrapper), contextlib.redirect_stdout(wrapper), contextlib.redirect_stderr(stderr):
                code = main(['--config', str(self.config_path), *args])
            wrapper.flush()
            return code, stdout.getvalue(), stderr.getvalue()
        finally:
            wrapper.close()
            input_wrapper.close()

    def test_get_help_and_missing_paths_usage_real_wire(self):
        result = run_cli(['get', '--help'])
        self.assertEqual(result.returncode, 0)
        for flag in (b'--force', b'--stdout', b'-o'):
            self.assertIn(flag, result.stdout)
        result = run_cli(['--config', str(self.config_path), 'get'])
        self.assertEqual(result.returncode, 2)

    def test_stdout_binary_pure_even_json_and_bad_hash_exit_four(self):
        path, _, snapshot, archive = self.archived(content=b'\x00\xff\nbytes')
        code, output, error = self.invoke('--json', 'get', archive, '--stdout')
        self.assertEqual((code, output, error), (0, path.read_bytes(), ''))
        self.engine.inject_corruption(snapshot, str(path), flip_byte=True)
        code, output, error = self.invoke('get', archive, '--stdout')
        self.assertEqual(code, 4)
        self.assertTrue(output)
        self.assertIn('hash', error)

    def test_multi_stdout_invalid_before_engine_and_unknown_exit_four(self):
        _, _, _, archive = self.archived()
        self.assertEqual(self.invoke('get', archive, archive, '--stdout')[0], 2)
        self.assertEqual(self.invoke('get', 'unknown', '-o', str(self.out))[0], 4)
        _, _, _, tree = self.archived('tree', tree=True)
        self.assertEqual(self.invoke('get', tree, '--stdout')[0], 2)

    def test_find_pipe_newline_and_nul_and_force_json(self):
        _, _, _, archive = self.archived('first.txt')
        _, _, _, other = self.archived('second.txt')
        found = run_cli(['--config', str(self.config_path), '-0', 'find'])
        self.assertEqual(found.returncode, 0, found.stderr)
        code, output, error = self.invoke('-0', '--json', 'get', '-', '-o', str(self.out), stdin=found.stdout)
        self.assertEqual(code, 0, error)
        self.assertEqual({r['outcome'] for r in map(json.loads, output.splitlines())}, {'restored'})
        self.assertEqual({p.name for p in self.out.iterdir()}, {'first.txt', 'second.txt'})
        self.assertEqual(self.invoke('get', archive, '-o', str(self.out))[0], 1)
        code, output, error = self.invoke('--json', 'get', '-', '--force', '-o', str(self.out), stdin=(archive + '\n' + other + '\n').encode())
        self.assertEqual(code, 0, error)
        for record in map(json.loads, output.splitlines()):
            self.assertTrue(Path(record['aside_path']).is_file())
        self.assertEqual(self.invoke('get', '-', stdin=b'')[0], 0)

    def test_darwin_public_get_accepts_arbitrary_destination_and_force(self):
        import os
        from dropin import retrieve as retrieve_module
        from dropin.cli import get as get_module

        path, _, _, archive = self.archived('Mac.app', tree=True, bundle=True)
        def move(source_fd, source, destination_fd, destination):
            os.rename(source, destination, src_dir_fd=source_fd,
                      dst_dir_fd=destination_fd)
        with patch.object(get_module.sys, 'platform', 'darwin'), \
             patch.object(retrieve_module, 'rename_noreplace', move):
            code, output, error = self.invoke(
                '--json', 'get', archive, '-o', str(self.out))
        self.assertEqual(code, 0, error)
        self.assertTrue(json.loads(output)['verified'])
        target = self.out / path.name
        self.assertEqual((target / 'sub/a').read_bytes(), b'verified bytes')
        (target / 'marker').write_bytes(b'existing')
        with patch.object(get_module.sys, 'platform', 'darwin'), \
             patch.object(retrieve_module, 'rename_noreplace', move):
            code, output, error = self.invoke(
                '--json', 'get', archive, '--force', '-o', str(self.out))
        self.assertEqual(code, 0, error)
        result = json.loads(output)
        self.assertTrue(result['verified'])
        self.assertEqual(Path(result['aside_path'], 'marker').read_bytes(), b'existing')

    def test_categorized_dump_failure_is_reported_in_json_without_partial_output(self):
        _, _, _, archive = self.archived()
        self.engine.fail_with("no-repo")
        code, output, error = self.invoke("--json", "get", archive, "-o", str(self.out))
        self.assertEqual(code, 3)
        result = json.loads(output)
        self.assertEqual(result["outcome"], "refused")
        self.assertEqual(result["error"], "no-repo")
        self.assertEqual(list(self.out.iterdir()), [])
        self.assertIn("no-repo", result["reason"])
        self.assertEqual(error, "")

    def test_tools_missing_before_remote_is_exit_three(self):
        _, _, _, archive = self.archived()
        result = run_cli(['--config', str(self.config_path), 'get', archive, '-o', str(self.out)])
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(list(self.out.iterdir()), [])

    def test_nul_stdin_preserves_newline_names_and_missing_does_not_block_later_item(self):
        path, _, _, archive = self.archived('line\nbreak.txt')
        code, output, error = self.invoke('-0', 'get', '-', '-o', str(self.out), stdin=b'unknown\0' + archive.encode() + b'\0')
        self.assertEqual(code, 4)
        self.assertIn('unknown', error)
        self.assertEqual((self.out / path.name).read_bytes(), path.read_bytes())

    def test_cleanup_error_remains_per_item_and_later_retrieval_continues(self):
        from dropin import retrieve as module
        path, _, snapshot, archive = self.archived('bad')
        good, _, _, next_archive = self.archived('good')
        self.engine.inject_corruption(snapshot, str(path), flip_byte=True)
        cleanup = module._cleanup
        calls = 0
        def denied_once(*args):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise PermissionError('cleanup denied')
            return cleanup(*args)
        with patch.object(module, '_cleanup', denied_once):
            code, output, error = self.invoke('--json', 'get', archive, next_archive, '-o', str(self.out))
        self.assertEqual(code, 4)
        records = list(map(json.loads, output.splitlines()))
        self.assertEqual([r['outcome'] for r in records], ['corrupt', 'restored'])
        self.assertIn('hash', records[0]['reason'])
        self.assertIn('cleanup denied', records[0]['reason'])
        self.assertEqual((self.out / good.name).read_bytes(), good.read_bytes())

    def test_mode000_collision_is_refusal_and_later_item_continues(self):
        from dropin import retrieve as module
        _, _, _, archive = self.archived('locked', tree=True, captured_dir_mode=0o40000)
        _, _, _, next_archive = self.archived('good')
        native = module.rename_noreplace
        def collide(sfd, source, dfd, destination):
            if destination == 'locked':
                (self.out / 'locked').mkdir()
            return native(sfd, source, dfd, destination)
        with patch.object(module, 'rename_noreplace', collide):
            code, output, error = self.invoke('--json', 'get', archive, next_archive, '-o', str(self.out))
        self.assertEqual(code, 1)
        self.assertEqual([r['outcome'] for r in map(json.loads, output.splitlines())], ['refused', 'restored'])
        self.assertEqual(list(self.out.glob('.dropin-restore-*')), [])
