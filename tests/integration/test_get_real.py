"""Real restic/rclone retrieval and authenticated pack-corruption refusal."""
import os
import plistlib
import unittest

from dropin.macos.interface import TAGS_XATTR
from tests.integration import test_restic_roundtrip as roundtrip
from tests.support import run_cli


@unittest.skipUnless(os.environ.get('DROPIN_RESTIC_BIN') and os.environ.get('DROPIN_RCLONE_BIN'),
                     'pinned tool environment not provided')
class RealGetTest(roundtrip.ResticTestCase):
    def get(self, *args, stdin=None):
        return run_cli(['--config', str(self.config_path), *args], stdin=stdin)

    def output(self):
        out = (self.root / 'out').resolve()
        out.mkdir()
        return out

    def test_file_tree_bundle_roundtrip_find_pipe_and_stdout(self):
        file = self.drop_file('note.txt', b'hello archive\n')
        pdf = self.drop_file(
            'tagged.pdf', b'%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n%%EOF\n')
        self.macos.set_xattr(
            str(pdf), TAGS_XATTR,
            plistlib.dumps(['ReleaseTag\n0'], fmt=plistlib.FMT_BINARY))
        tree = self.drop_tree('tree')
        bundle = self.drop_tree('Document.pages')
        self.macos.set_mdls(str(bundle), 'kMDItemContentTypeTree = (\n    "com.apple.package"\n)\n')
        report = self.run_drain()
        self.assertEqual(report.exit_code(), 0, report.render_human())
        for path in (file, pdf, tree, bundle): self.assertFalse(path.exists())
        self.assertEqual(self.occurrence(bundle.name)['kind'], 'bundle')
        out = self.output()
        found = self.get('find', '--name', 'note')
        restored = self.get('get', '-', '-o', str(out), stdin=found.stdout)
        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertEqual((out / file.name).read_bytes(), b'hello archive\n')
        (out / file.name).write_bytes(b'existing destination\n')
        forced = self.get('get', self.occurrence(file.name)['archive_path'],
                          '--force', '-o', str(out))
        self.assertEqual(forced.returncode, 0, forced.stderr)
        self.assertEqual((out / file.name).read_bytes(), b'hello archive\n')
        [aside] = list(out.glob('.dropin-aside-*'))
        self.assertEqual((aside / file.name).read_bytes(), b'existing destination\n')
        stdout = self.get('get', self.occurrence(file.name)['archive_path'], '--stdout')
        self.assertEqual((stdout.returncode, stdout.stdout, stdout.stderr), (0, b'hello archive\n', b''))
        tagged = self.get('find', '--tag', 'ReleaseTag')
        self.assertEqual(tagged.returncode, 0, tagged.stderr)
        self.assertIn(self.occurrence(pdf.name)['archive_path'].encode(), tagged.stdout)
        result = self.get('get', self.occurrence(pdf.name)['archive_path'],
                          '-o', str(out))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((out / pdf.name).read_bytes(),
                         b'%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n%%EOF\n')
        for path in (tree, bundle):
            archive = self.occurrence(path.name)['archive_path']
            if path == bundle: archive += '/sub/b.txt'
            result = self.get('get', archive, '-o', str(out))
            self.assertEqual(result.returncode, 0, result.stderr)
            target = out / path.name
            self.assertEqual((target / 'a.txt').read_bytes(), b'alpha\n' * 100)
            self.assertEqual((target / 'sub/b.txt').read_bytes(), b'beta\n' * 100)
            self.assertEqual(os.readlink(target / 'link'), 'a.txt')
            self.assertTrue((target / 'empty').is_dir())
        self.assertEqual(list(out.glob('.dropin-restore-*')), [])

    def test_corrupt_payload_pack_exit_four_no_partial_output_existing_untouched(self):
        path = self.drop_file('corrupt.bin', os.urandom(65536))
        self.assertEqual(self.run_drain().exit_code(), 0)
        occurrence = self.occurrence(path.name)
        attempt = roundtrip.records.get_attempt(self.db, occurrence['confirmed_attempt_id'])
        blob = self.engine.node_content_ids(attempt['snapshot_id'], str(path))[0]
        roundtrip.CatalogCorruptionTest._flip_byte_in_blob(self, blob)
        out = self.output()
        before = list(self.db.iterdump())
        result = self.get('get', occurrence['archive_path'], '-o', str(out))
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertEqual(list(out.iterdir()), [])
        (out / path.name).write_bytes(b'existing original')
        result = self.get('get', occurrence['archive_path'], '-o', str(out), '--force')
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertEqual((out / path.name).read_bytes(), b'existing original')
        self.assertEqual(list(out.iterdir()), [out / path.name])
        self.assertEqual(before, list(self.db.iterdump()))
