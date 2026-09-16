"""Safe retrieval and adversarial tar streams; originals are disposable."""
import contextlib
import io
import os
from pathlib import Path
import stat
import tarfile
from unittest.mock import patch

from tests.retrieve_support import RetrieveTestCase


class RetrieveTest(RetrieveTestCase):
    def test_file_temp_verified_mode_and_no_store_mutation(self):
        path, occ, snapshot, archive = self.archived()
        before = list(self.db.iterdump())
        real_dump = self.engine.dump
        @contextlib.contextmanager
        def observe(*args, **kwargs):
            with real_dump(*args, **kwargs) as stream:
                self.assertFalse((self.out / path.name).exists())
                stages = list(self.out.glob('.dropin-restore-*'))
                self.assertEqual(len(stages), 1)
                self.assertEqual(stat.S_IMODE(stages[0].stat().st_mode), 0o700)
                yield stream
                self.assertFalse((self.out / path.name).exists())
        with patch.object(self.engine, 'dump', observe):
            result = self.restore(archive)
        self.assertEqual(Path(result['written_path']).read_bytes(), path.read_bytes())
        self.assertTrue(result['verified'])
        self.assertEqual(stat.S_IMODE((self.out / path.name).stat().st_mode), 0o640)
        self.assertEqual(list(self.out.iterdir()), [self.out / path.name])
        self.assertEqual(list(self.db.iterdump()), before)

    def test_corrupt_and_late_engine_exit_preserve_existing_with_force(self):
        from dropin.retrieve import RetrieveError
        path, _, snapshot, archive = self.archived()
        target = self.out / path.name
        target.write_bytes(b'original')
        real_dump = self.engine.dump
        @contextlib.contextmanager
        def late(*args, **kwargs):
            with real_dump(*args, **kwargs) as stream:
                yield stream
            from dropin.engine.interface import EngineError
            raise EngineError('corrupt', 'late nonzero exit')
        for corrupt in (False, True):
            with self.subTest(corrupt=corrupt):
                if corrupt:
                    self.engine.inject_corruption(snapshot, str(path), flip_byte=True)
                with patch.object(self.engine, 'dump', late), self.assertRaises(RetrieveError):
                    self.restore(archive, force=True)
                self.assertEqual(target.read_bytes(), b'original')
                self.assertEqual(list(self.out.iterdir()), [target])

    def test_existing_destination_refuses_and_force_retains_aside(self):
        from dropin.retrieve import RetrieveError
        path, _, _, archive = self.archived()
        target = self.out / path.name
        target.symlink_to('/nonexistent/external')
        with self.assertRaises(RetrieveError):
            self.restore(archive)
        result = self.restore(archive, force=True)
        aside = Path(result['aside_path'])
        self.assertEqual(os.readlink(aside), '/nonexistent/external')
        self.assertEqual(stat.S_IMODE(aside.parent.stat().st_mode), 0o700)
        self.assertEqual(target.read_bytes(), path.read_bytes())

    def test_tree_bundle_internal_subtree_and_standalone_symlink(self):
        for bundle in (False, True):
            path, _, _, archive = self.archived('bundle' if bundle else 'tree', tree=True, bundle=bundle)
            result = self.restore(archive + '/sub/a' if bundle else archive)
            target = Path(result['written_path'])
            self.assertEqual((target / 'sub/a').read_bytes(), b'verified bytes')
            self.assertEqual(os.readlink(target / 'link'), 'sub/a')
            self.assertTrue((target / 'empty').is_dir())
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o750)
        result = self.restore(archive + '/link', force=True)
        self.assertEqual(result['kind'], 'bundle')
        ordinary = self.db.execute("SELECT archive_path FROM occurrence WHERE kind='dir'").fetchone()[0]
        self.assertEqual(Path(self.restore(ordinary + '/sub')['written_path']).name, 'sub')
        link = Path(self.restore(ordinary + '/link')['written_path'])
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.readlink(link), 'sub/a')

    def test_stdout_streams_before_check_and_rejects_tree(self):
        from dropin.retrieve import RetrieveError
        path, _, snapshot, archive = self.archived()
        out = io.BytesIO()
        self.restore(archive, stdout=out)
        self.assertEqual(out.getvalue(), path.read_bytes())
        self.engine.inject_corruption(snapshot, str(path), flip_byte=True)
        out = io.BytesIO()
        with self.assertRaises(RetrieveError):
            self.restore(archive, stdout=out)
        self.assertTrue(out.getvalue())
        _, _, _, tree = self.archived('tree', tree=True)
        with self.assertRaises(RetrieveError):
            self.restore(tree, stdout=io.BytesIO())
        self.assertEqual(list(self.out.iterdir()), [])

    def test_only_confirmed_attempts_and_valid_manifest_hash(self):
        from dropin.retrieve import RetrieveError
        _, occ, _, archive = self.archived('tree', tree=True)
        self.db.execute("UPDATE occurrence SET root_sha256=? WHERE occ_id=?", ('0' * 64, occ))
        with self.assertRaises(RetrieveError):
            self.restore(archive)
        self.db.execute("UPDATE occurrence SET confirmed_attempt_id=NULL WHERE occ_id=?", (occ,))
        with self.assertRaises(RetrieveError):
            self.restore(archive)
        self.assertEqual(list(self.out.iterdir()), [])

    def test_destination_must_exist_and_no_implicit_mkdir(self):
        from dropin.retrieve import RetrieveError, retrieve
        _, _, _, archive = self.archived()
        with self.assertRaises(RetrieveError):
            retrieve(self.db, self.engine, archive, self.out / 'absent')
        self.assertEqual(list(self.out.iterdir()), [])

    def test_force_publication_collision_never_overwrites_concurrent_target(self):
        from dropin import retrieve as module
        path, _, _, archive = self.archived()
        target = self.out / path.name
        target.write_bytes(b'original')
        native = module.rename_noreplace
        calls = []
        def collision(*args):
            calls.append(args)
            if len(calls) == 2:
                target.write_bytes(b'concurrent')
            return native(*args)
        with patch.object(module, 'rename_noreplace', collision), self.assertRaises(module.RetrieveError) as caught:
            self.restore(archive, force=True)
        self.assertEqual(target.read_bytes(), b'concurrent')
        [aside] = list(self.out.glob('.dropin-aside-*/' + path.name))
        self.assertEqual(aside.read_bytes(), b'original')
        self.assertIn(str(aside), str(caught.exception))
        self.assertEqual(list(self.out.glob('.dropin-restore-*')), [])

    def test_force_publication_failure_rolls_back_original(self):
        from dropin import retrieve as module
        path, _, _, archive = self.archived()
        target = self.out / path.name
        target.write_bytes(b'original')
        native = module.rename_noreplace
        calls = []
        def fail(*args):
            calls.append(args)
            if len(calls) == 2:
                raise OSError('publication failure')
            return native(*args)
        with patch.object(module, 'rename_noreplace', fail), self.assertRaises(module.RetrieveError):
            self.restore(archive, force=True)
        self.assertEqual(target.read_bytes(), b'original')
        self.assertEqual(list(self.out.iterdir()), [target])


class AdversarialTarTest(RetrieveTestCase):
    def test_adversarial_member_streams_clean_up_and_touch_no_external_paths(self):
        from dropin.retrieve import RetrieveError
        tree, _, snapshot, archive = self.archived('tree', tree=True)
        with self.engine.dump(snapshot, str(tree), archive='tar') as stream:
            original = stream.read()
        with tarfile.open(fileobj=io.BytesIO(original)) as tf:
            members = [(m, tf.extractfile(m).read() if m.isreg() else None) for m in tf]
        # Build altered streams without extract/extractall, using only fake bytes.
        def encoded(items):
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode='w') as tf:
                for m, data in items:
                    tf.addfile(m, io.BytesIO(data) if data is not None else None)
            return buf.getvalue()
        prefix = str(tree).lstrip('/')
        def extra(name, type=tarfile.REGTYPE, link=''):
            m = tarfile.TarInfo(name)
            m.type, m.linkname = type, link
            return m, b'' if type == tarfile.REGTYPE else None
        file_index = next(i for i, (m, _) in enumerate(members) if m.isreg())
        corrupted = list(members)
        m, data = corrupted[file_index]
        corrupted[file_index] = (m, b'x' * len(data))
        missing = [pair for i, pair in enumerate(members) if i != file_index]
        # Remove both terminating zero blocks on an exact member boundary.
        last = max(m.offset_data + ((m.size + 511) // 512) * 512 for m, _ in members)
        cases = {
            'late hash': encoded(corrupted), 'missing': encoded(missing),
            'truncated body': original[:last - 2], 'missing terminator': original[:last],
            'one zero block': original[:last + 512],
            'nonzero trailing': original + b'bad',
            'member after eof': original + encoded([extra(prefix + '/after')]),
            'absolute': encoded(members + [extra(str(self.root / 'escape'))]),
            'traversal': encoded(members + [extra(prefix + '/../escape')]),
            'dot': encoded(members + [extra(prefix + '/./bad')]),
            'empty segment': encoded(members + [extra(prefix + '//bad')]),
            'duplicate': encoded(members + [members[file_index]]),
            'symlink ancestor': encoded(members + [extra(prefix + '/link/escape')]),
            'hardlink': encoded(members + [extra(prefix + '/hard', tarfile.LNKTYPE, 'sub/a')]),
            'fifo': encoded(members + [extra(prefix + '/fifo', tarfile.FIFOTYPE)]),
            'extra': encoded(members + [extra(prefix + '/extra')]),
        }
        outside = self.root / 'escape'
        outside.write_bytes(b'untouched')
        target = self.out / 'tree'
        target.mkdir()
        (target / 'original').write_bytes(b'original')
        for label, payload in cases.items():
            @contextlib.contextmanager
            def dump(*args, **kwargs):
                yield io.BytesIO(payload)
            with self.subTest(label=label), patch.object(self.engine, 'dump', dump):
                with self.assertRaises(RetrieveError):
                    self.restore(archive, force=True)
                self.assertEqual(outside.read_bytes(), b'untouched')
                self.assertEqual(list(self.out.iterdir()), [target])
                self.assertEqual((target / 'original').read_bytes(), b'original')


class RetrieveBoundaryTest(RetrieveTestCase):
    def test_stage_name_is_namespaced_unique_and_stale_stage_never_touched(self):
        path, occ, _, archive = self.archived()
        stale = self.out / ('.dropin-restore-' + occ + '-stale')
        stale.mkdir(mode=0o700)
        (stale / 'keep').write_bytes(b'private leftover')
        real_dump = self.engine.dump
        @contextlib.contextmanager
        def check(*args, **kwargs):
            stages = list(self.out.glob('.dropin-restore-' + occ + '-*'))
            self.assertEqual(len(stages), 2)
            with real_dump(*args, **kwargs) as stream:
                yield stream
        with patch.object(self.engine, 'dump', check):
            self.restore(archive)
        self.assertEqual((stale / 'keep').read_bytes(), b'private leftover')
        self.assertEqual(set(self.out.iterdir()), {stale, self.out / path.name})

    def test_result_hash_meanings_file_root_subtree_bundle_and_symlink(self):
        import hashlib
        import json
        from dropin.capture.extract import MANIFEST_FIELDS
        file, _, _, archive = self.archived()
        self.assertEqual(self.restore(archive)['sha256'], hashlib.sha256(file.read_bytes()).hexdigest())
        _, occ, _, archive = self.archived('tree', tree=True)
        root_hash = self.db.execute('SELECT root_sha256 FROM occurrence WHERE occ_id=?', (occ,)).fetchone()[0]
        self.assertEqual(self.restore(archive)['sha256'], root_hash)
        digest = hashlib.sha256()
        for row in self.db.execute("SELECT * FROM entry WHERE occ_id=? AND (rel_path='sub' OR rel_path='sub/a') ORDER BY rel_path", (occ,)):
            digest.update(json.dumps({f: row[f] for f in MANIFEST_FIELDS}, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode())
            digest.update(b'\n')
        self.assertEqual(self.restore(archive + '/sub')['sha256'], digest.hexdigest())
        self.assertIsNone(self.restore(archive + '/link')['sha256'])
        _, occ, _, archive = self.archived('bundle', tree=True, bundle=True)
        root_hash = self.db.execute('SELECT root_sha256 FROM occurrence WHERE occ_id=?', (occ,)).fetchone()[0]
        result = self.restore(archive + '/sub/a')
        self.assertEqual(result['sha256'], root_hash)
        self.assertEqual(result['kind'], 'bundle')

    def test_empty_file_empty_directory_restrictive_modes_and_no_follow_links(self):
        _, _, _, archive = self.archived(content=b'')
        self.assertEqual(Path(self.restore(archive)['written_path']).read_bytes(), b'')
        path, occ, _, archive = self.archived('tree', tree=True, captured_dir_mode=0o40000)
        result = self.restore(archive)
        target = Path(result['written_path'])
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0)
        # Restore test-fixture access so the outer temporary directory can clean.
        target.chmod(0o700)
        (target / 'sub').chmod(0o700)
        (target / 'empty').chmod(0o700)
        self.assertEqual((target / 'sub/a').read_bytes(), b'verified bytes')

    def test_late_tree_engine_failure_and_disk_failure_clean_stage(self):
        from dropin.retrieve import RetrieveError
        from dropin.engine.interface import EngineError
        _, _, _, archive = self.archived('tree', tree=True)
        real_dump = self.engine.dump
        @contextlib.contextmanager
        def late(*args, **kwargs):
            with real_dump(*args, **kwargs) as stream:
                yield stream
            raise EngineError('corrupt', 'nonzero after complete tar')
        with patch.object(self.engine, 'dump', late), self.assertRaises(RetrieveError):
            self.restore(archive)
        self.assertEqual(list(self.out.iterdir()), [])
        with patch('dropin.retrieve.os.fsync', side_effect=OSError('disk full')), self.assertRaises(RetrieveError):
            self.restore(archive)
        self.assertEqual(list(self.out.iterdir()), [])

    def test_nonforce_publication_collision_keeps_concurrent_destination(self):
        from dropin import retrieve as module
        path, _, _, archive = self.archived()
        target = self.out / path.name
        native = module.rename_noreplace
        def collide(*args):
            target.write_bytes(b'concurrent')
            return native(*args)
        with patch.object(module, 'rename_noreplace', collide), self.assertRaises(module.RetrieveError):
            self.restore(archive)
        self.assertEqual(target.read_bytes(), b'concurrent')
        self.assertEqual(list(self.out.iterdir()), [target])

    def test_mismatched_types_link_target_size_and_invalid_late_header(self):
        from dropin.retrieve import RetrieveError
        tree, _, snapshot, archive = self.archived('tree', tree=True)
        with self.engine.dump(snapshot, str(tree), archive='tar') as stream:
            original = stream.read()
        for kind in ('hardlink', 'fifo', 'wrong link', 'size', 'bad late header'):
            buf = io.BytesIO()
            with tarfile.open(fileobj=io.BytesIO(original)) as source, tarfile.open(fileobj=buf, mode='w') as output:
                for m in source:
                    data = None
                    if m.isreg():
                        with source.extractfile(m) as member:
                            data = member.read()
                        if kind in ('hardlink', 'fifo'):
                            m.type = tarfile.LNKTYPE if kind == 'hardlink' else tarfile.FIFOTYPE
                            m.linkname, m.size, data = 'sub/a', 0, None
                        if kind == 'size':
                            data += b'x'
                            m.size += 1
                    if m.issym() and kind == 'wrong link':
                        m.linkname = str(self.root / 'outside')
                    output.addfile(m, io.BytesIO(data) if data is not None else None)
            payload = buf.getvalue()
            if kind == 'bad late header':
                with tarfile.open(fileobj=io.BytesIO(payload)) as source:
                    end = max(m.offset_data + ((m.size + 511) // 512) * 512 for m in source)
                payload = payload[:end] + b'x' * 512 + payload[end:]
            @contextlib.contextmanager
            def dump(*args, **kwargs):
                yield io.BytesIO(payload)
            with self.subTest(kind=kind), patch.object(self.engine, 'dump', dump), self.assertRaises(RetrieveError):
                self.restore(archive)
            self.assertEqual(list(self.out.iterdir()), [])

    def test_readonly_tree_modes_and_collision_cleanup(self):
        from dropin import retrieve as module
        _, _, _, archive = self.archived('readonly', tree=True, captured_dir_mode=0o40555)
        target = self.out / 'readonly'
        native = module.rename_noreplace
        def collide(*args):
            target.mkdir()
            (target / 'keep').write_bytes(b'external')
            return native(*args)
        with patch.object(module, 'rename_noreplace', collide), self.assertRaises(module.RetrieveError):
            self.restore(archive)
        self.assertEqual(list(self.out.iterdir()), [target])
        self.assertEqual((target / 'keep').read_bytes(), b'external')
        (target / 'keep').unlink()
        target.rmdir()
        self.restore(archive)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o555)
        self.assertEqual(stat.S_IMODE((target / 'sub').stat().st_mode), 0o555)
        target.chmod(0o700)
        (target / 'sub').chmod(0o700)
        (target / 'empty').chmod(0o700)

    def test_empty_root_tree_and_external_symlink_never_dereferenced(self):
        _, _, _, archive = self.archived('empty-tree', empty_tree=True)
        target = Path(self.restore(archive)['written_path'])
        self.assertEqual(list(target.iterdir()), [])
        tree, occ, snapshot, archive = self.archived('external-links', tree=True)
        # A genuine archived absolute symlink, not a fake target rewrite.
        (tree / 'link').unlink()
        outside = self.root / 'outside'
        outside.write_bytes(b'never changed')
        (tree / 'link').symlink_to(outside)
        self.db.execute("UPDATE occurrence SET state='recorded',confirmed_attempt_id=NULL WHERE occ_id=?", (occ,))
        # Re-capture a new immutable occurrence instead of editing entry evidence.
        from dropin.store import records
        from dropin.engine.interface import Identity
        occ = self.record(tree)
        snapshot = self.publish(tree)
        attempt = records.start_attempt(self.db, occ, self.store_id, export_path='/unused')
        records.set_attempt_snapshot(self.db, attempt.attempt_id, snapshot)
        records.finish_attempt(self.db, attempt.attempt_id, 'confirmed')
        records.observe_snapshot(self.db, snapshot, Identity(self.store_id, occ, attempt.attempt_id,
            attempt.export_seq, 'dir', 'a' * 64), status='confirmed')
        self.db.execute("UPDATE occurrence SET confirmed_attempt_id=?,state='evicted' WHERE occ_id=?", (attempt.attempt_id, occ))
        archive = records.get_occurrence(self.db, occ)['archive_path']
        result = self.restore(archive + '/link')
        self.assertEqual(os.readlink(result['written_path']), str(outside))
        self.assertEqual(outside.read_bytes(), b'never changed')

    def test_catalog_root_name_must_be_one_basename(self):
        from dropin.retrieve import RetrieveError
        _, occ, _, archive = self.archived()
        self.db.execute("UPDATE occurrence SET item_name='nested/file' WHERE occ_id=?", (occ,))
        with self.assertRaises(RetrieveError) as caught:
            self.restore(archive)
        self.assertEqual(caught.exception.kind, 'corrupt')
        self.assertEqual(list(self.out.iterdir()), [])
