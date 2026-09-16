"""Regressions for the retrieval findings summarized in phase5-validation.md."""
import contextlib
import os
from pathlib import Path
import stat
import sys
import unittest
from unittest.mock import patch

from dropin import retrieve as module
from dropin import retrieve_fs
from tests.retrieve_support import RetrieveTestCase


class RetrieveReviewTest(RetrieveTestCase):
    def _replace_stage_before_first_open(self):
        original_open = module.Destination.open_fd
        moved = self.out / 'owned-moved'
        replacement = []

        def opening(destination, name, flags, **kwargs):
            if str(name).startswith('.dropin-restore-') and not replacement:
                created = self.out / name
                created.rename(moved)
                created.mkdir(mode=0o700)
                (created / 'keep').write_bytes(b'concurrent')
                replacement.append(created)
            return original_open(destination, name, flags, **kwargs)

        return opening, moved, replacement

    def _replace_owned_directory_after_mkdir(self, prefix, *, empty=False):
        native_mkdir = os.mkdir
        moved = self.out / (prefix.strip('.-') + '-created-moved')
        replacement = []

        def making(name, mode=0o777, *, dir_fd=None):
            result = native_mkdir(name, mode, dir_fd=dir_fd)
            if str(name).startswith(prefix) and not replacement:
                created = self.out / name
                created.rename(moved)
                native_mkdir(created, 0o700)
                if not empty:
                    (created / 'keep').write_bytes(b'concurrent')
                replacement.append(created)
            return result

        return making, moved, replacement

    def test_stage_substitution_during_acquisition_is_not_published_as_verified(self):
        _, _, _, archive = self.archived('tree', tree=True)
        opening, moved, replacement = self._replace_stage_before_first_open()
        with patch.object(module.Destination, 'open_fd', opening), \
             self.assertRaises(module.RetrieveError) as caught:
            self.restore(archive)
        self.assertEqual(caught.exception.kind, 'refused')
        self.assertIn('acquisition', str(caught.exception))
        self.assertEqual((moved).stat().st_mode & 0o777, 0o700)
        self.assertEqual(len(replacement), 1)
        self.assertEqual((replacement[0] / 'keep').read_bytes(), b'concurrent')
        self.assertFalse((self.out / 'tree').exists())

    def test_stage_substitution_during_acquisition_is_not_deleted_on_corruption(self):
        path, _, snapshot, archive = self.archived('tree', tree=True)
        self.engine.inject_corruption(snapshot, str(path / 'sub/a'), flip_byte=True)
        opening, moved, replacement = self._replace_stage_before_first_open()
        with patch.object(module.Destination, 'open_fd', opening), \
             self.assertRaises(module.RetrieveError) as caught:
            self.restore(archive)
        self.assertTrue(moved.is_dir())
        self.assertEqual(len(replacement), 1)
        self.assertEqual((replacement[0] / 'keep').read_bytes(), b'concurrent')
        self.assertFalse((self.out / 'tree').exists())
        self.assertEqual(caught.exception.kind, 'refused')
        self.assertIn('acquisition', str(caught.exception))

    def test_stage_substitution_immediately_after_mkdir_is_not_published(self):
        _, _, _, archive = self.archived('tree', tree=True)
        making, moved, replacement = self._replace_owned_directory_after_mkdir('.dropin-restore-')
        with patch.object(retrieve_fs.os, 'mkdir', making), \
             self.assertRaises(module.RetrieveError) as caught:
            self.restore(archive)
        self.assertEqual(caught.exception.kind, 'refused')
        self.assertIn('acquisition', str(caught.exception))
        self.assertTrue(moved.is_dir())
        self.assertEqual((replacement[0] / 'keep').read_bytes(), b'concurrent')
        self.assertFalse((self.out / 'tree').exists())

    def test_stage_substitution_immediately_after_mkdir_is_not_cleaned_on_corruption(self):
        path, _, snapshot, archive = self.archived('tree', tree=True)
        self.engine.inject_corruption(snapshot, str(path / 'sub/a'), flip_byte=True)
        making, moved, replacement = self._replace_owned_directory_after_mkdir('.dropin-restore-')
        with patch.object(retrieve_fs.os, 'mkdir', making), \
             self.assertRaises(module.RetrieveError) as caught:
            self.restore(archive)
        self.assertEqual((replacement[0] / 'keep').read_bytes(), b'concurrent')
        self.assertTrue(moved.is_dir())
        self.assertFalse((self.out / 'tree').exists())
        self.assertEqual(caught.exception.kind, 'refused')
        self.assertIn('acquisition', str(caught.exception))

    def test_force_aside_substitution_during_acquisition_moves_nothing(self):
        _, _, _, archive = self.archived()
        target = self.out / 'file.txt'
        target.write_bytes(b'original')
        making, moved, replacement = self._replace_owned_directory_after_mkdir('.dropin-aside-')
        with patch.object(retrieve_fs.os, 'mkdir', making), \
             self.assertRaises(module.RetrieveError) as caught:
            self.restore(archive, force=True)
        self.assertEqual(caught.exception.kind, 'refused')
        self.assertIn('acquisition', str(caught.exception))
        self.assertEqual(target.read_bytes(), b'original')
        self.assertTrue(moved.is_dir())
        self.assertEqual((replacement[0] / 'keep').read_bytes(), b'concurrent')
        self.assertEqual(list(self.out.glob('.dropin-restore-*')), [])

    @unittest.skipUnless(sys.platform == 'linux',
                         'requires the Linux acquisition provenance guard')
    def test_empty_lookalike_substitution_before_first_observation_is_refused(self):
        for aside in (False, True):
            with self.subTest(aside=aside):
                name = f'empty-lookalike-{aside}'
                _, _, _, archive = self.archived(name, tree=True)
                self.out = self.root / ('out-' + name)
                self.out.mkdir()
                target = self.out / name
                if aside:
                    target.write_bytes(b'original')
                prefix = '.dropin-aside-' if aside else '.dropin-restore-'
                making, moved, replacement = self._replace_owned_directory_after_mkdir(prefix, empty=True)
                with patch.object(retrieve_fs.os, 'mkdir', making), \
                     self.assertRaises(module.RetrieveError) as caught:
                    self.restore(archive, force=aside)
                self.assertEqual(caught.exception.kind, 'refused')
                self.assertIn('acquisition', str(caught.exception))
                self.assertEqual(list(moved.iterdir()), [])
                self.assertEqual(list(replacement[0].iterdir()), [])
                if aside:
                    self.assertEqual(target.read_bytes(), b'original')
                else:
                    self.assertFalse(target.exists())

    def test_failed_acquisition_closes_fds_without_modifying_candidate(self):
        _, _, _, archive = self.archived('tree', tree=True)
        open_fd = module.Destination.open_fd
        candidates = []
        def opening(destination, name, flags, **kwargs):
            fd = open_fd(destination, name, flags, **kwargs)
            if str(name).startswith('.dropin-restore-'):
                candidates.append(fd)
            return fd
        with patch.object(module.Destination, 'open_fd', opening), \
             patch.object(retrieve_fs.AcquisitionGuard, 'verify', side_effect=OSError('watch lost')), \
             patch.object(os, 'fchmod') as fchmod, patch.object(os, 'chmod') as chmod, \
             patch.object(os, 'unlink') as unlink, patch.object(os, 'rmdir') as rmdir, \
             patch.object(self.engine, 'dump') as dump, \
             self.assertRaises(module.RetrieveError) as caught:
            self.restore(archive)
        for operation in (fchmod, chmod, unlink, rmdir, dump):
            operation.assert_not_called()
        self.assertIn('acquisition', str(caught.exception))
        self.assertIn('retained', str(caught.exception))
        self.assertIn('uncertain', str(caught.exception))
        self.assertEqual(len(candidates), 1)
        with self.assertRaises(OSError):
            os.fstat(candidates[0])
        [stage] = self.out.glob('.dropin-restore-*')
        self.assertEqual(list(stage.iterdir()), [])
        self.assertEqual(stat.S_IMODE(stage.stat().st_mode), 0o700)
        self.assertFalse((self.out / 'tree').exists())

    def test_darwin_no_force_restore_bypasses_linux_watcher(self):
        from dropin import retrieve_acquire
        _, _, _, archive = self.archived('Mac.app', tree=True, bundle=True)
        def move(source_fd, source, destination_fd, destination):
            os.rename(source, destination, src_dir_fd=source_fd,
                      dst_dir_fd=destination_fd)
        with patch.object(retrieve_acquire.sys, 'platform', 'darwin'), \
             patch.object(module, 'rename_noreplace', move):
            result = self.restore(archive)
        self.assertTrue(result['verified'])
        self.assertEqual((self.out / 'Mac.app/sub/a').read_bytes(), b'verified bytes')
        self.assertEqual(list(self.out.glob('.dropin-restore-*')), [])

    def test_darwin_force_uses_existing_aside_protocol(self):
        from dropin import retrieve_acquire
        _, _, _, archive = self.archived()
        target = self.out / 'file.txt'
        target.write_bytes(b'existing')
        def move(source_fd, source, destination_fd, destination):
            os.rename(source, destination, src_dir_fd=source_fd,
                      dst_dir_fd=destination_fd)
        with patch.object(retrieve_acquire.sys, 'platform', 'darwin'), \
             patch.object(module, 'rename_noreplace', move):
            result = self.restore(archive, force=True)
        self.assertEqual(target.read_bytes(), b'verified bytes')
        self.assertEqual(Path(result['aside_path']).read_bytes(), b'existing')

    def test_unsupported_acquisition_refuses_before_staging_and_stdout_still_works(self):
        import io
        from dropin import retrieve_acquire
        _, _, _, archive = self.archived()
        with patch.object(retrieve_acquire.sys, 'platform', 'freebsd'):
            with self.assertRaises(module.RetrieveError) as caught:
                self.restore(archive)
            self.assertIn('acquisition', str(caught.exception))
            self.assertEqual(list(self.out.iterdir()), [])
            output = io.BytesIO()
            self.assertTrue(self.restore(archive, stdout=output)['verified'])
            self.assertEqual(output.getvalue(), b'verified bytes')

    def test_mode000_collision_cleanup_preserves_primary_and_concurrent_data(self):
        _, _, _, archive = self.archived('locked', tree=True, captured_dir_mode=0o40000)
        target = self.out / 'locked'
        native = module.rename_noreplace
        def collide(*args):
            target.mkdir()
            (target / 'keep').write_bytes(b'concurrent')
            return native(*args)
        with patch.object(module, 'rename_noreplace', collide), self.assertRaises(module.RetrieveError) as caught:
            self.restore(archive)
        self.assertEqual(caught.exception.kind, 'refused')
        self.assertIn('exist', str(caught.exception).lower())
        self.assertEqual(list(self.out.iterdir()), [target])
        self.assertEqual((target / 'keep').read_bytes(), b'concurrent')

    def test_cleanup_failure_preserves_corruption_kind_and_reports_leftover(self):
        path, _, snapshot, archive = self.archived()
        self.engine.inject_corruption(snapshot, str(path), flip_byte=True)
        with patch.object(module, '_cleanup', side_effect=PermissionError('cleanup denied')), self.assertRaises(module.RetrieveError) as caught:
            self.restore(archive)
        self.assertEqual(caught.exception.kind, 'corrupt')
        self.assertIn('hash', str(caught.exception))
        self.assertIn('cleanup denied', str(caught.exception))
        self.assertIn('.dropin-restore-', str(caught.exception))

    def test_cleanup_failure_after_publication_is_refusal_with_retained_artifact(self):
        path, _, _, archive = self.archived()
        with patch.object(module, '_cleanup', side_effect=PermissionError('cleanup denied')), self.assertRaises(module.RetrieveError) as caught:
            self.restore(archive)
        self.assertEqual(caught.exception.kind, 'refused')
        self.assertIn('cleanup denied', str(caught.exception))
        self.assertIn('retained', str(caught.exception))
        self.assertIn(path.name, str(caught.exception))
        self.assertEqual((self.out / path.name).read_bytes(), path.read_bytes())

    def test_destination_and_ancestor_replacement_never_report_false_success(self):
        # Hooks cover verification, the pre-publication boundary, both sides of
        # native rename, and both sides of cleanup (including postpublication).
        for tree in (False, True):
            _, _, _, archive = self.archived('tree' if tree else 'file', tree=tree)
            for force in (False, True):
                for ancestor in (False, True):
                    for phase in ('dump-before', 'dump-after', 'publish', 'native-before', 'native-after', 'native-publish-before', 'native-publish-after', 'cleanup-before', 'cleanup-after'):
                        with self.subTest(tree=tree, force=force, ancestor=ancestor, phase=phase):
                            parent = self.root / f'case-{tree}-{force}-{ancestor}-{phase}'
                            parent.mkdir()
                            self.out = parent / 'out'
                            self.out.mkdir()
                            name = 'tree' if tree else 'file'
                            if force:
                                (self.out / name).write_bytes(b'original')
                            moved = parent.with_name(parent.name + '-old') if ancestor else parent / 'out-old'
                            old_out = moved / 'out' if ancestor else moved
                            swapped = False
                            def swap():
                                nonlocal swapped
                                if swapped:
                                    return
                                swapped = True
                                (parent if ancestor else self.out).rename(moved)
                                self.out.mkdir(parents=True)
                                (self.out / 'sentinel').write_bytes(b'concurrent')
                            native = module.rename_noreplace
                            publish, cleanup, dump = module._publish, module._cleanup, self.engine.dump
                            @contextlib.contextmanager
                            def dumping(*args, **kwargs):
                                with dump(*args, **kwargs) as stream:
                                    if phase == 'dump-before': swap()
                                    yield stream
                                    if phase == 'dump-after': swap()
                            def publishing(*args, **kwargs):
                                if phase == 'publish': swap()
                                return publish(*args, **kwargs)
                            rename_calls = 0
                            def renaming(*args, **kwargs):
                                nonlocal rename_calls
                                rename_calls += 1
                                publication = rename_calls == (2 if force else 1)
                                if publication and phase == 'native-publish-before': swap()
                                if phase == 'native-before': swap()
                                result = native(*args, **kwargs)
                                if phase == 'native-after': swap()
                                if publication and phase == 'native-publish-after': swap()
                                return result
                            def cleaning(*args, **kwargs):
                                if phase == 'cleanup-before': swap()
                                result = cleanup(*args, **kwargs)
                                if phase == 'cleanup-after': swap()
                                return result
                            with patch.object(self.engine, 'dump', dumping), patch.object(module, '_publish', publishing), patch.object(module, 'rename_noreplace', renaming), patch.object(module, '_cleanup', cleaning), self.assertRaises(module.RetrieveError) as caught:
                                self.restore(archive, force=force)
                            self.assertTrue(swapped)
                            self.assertEqual(caught.exception.kind, 'refused')
                            self.assertIn('destination', str(caught.exception))
                            self.assertIn('uncertain', str(caught.exception))
                            self.assertEqual(list(self.out.iterdir()), [self.out / 'sentinel'])
                            self.assertEqual((self.out / 'sentinel').read_bytes(), b'concurrent')
                            self.assertEqual(list(old_out.glob('.dropin-restore-*')), [])
                            if phase.startswith(('cleanup', 'native-publish')) or (phase.startswith('native') and not force):
                                self.assertIn('retained', str(caught.exception))
                                restored = old_out / name
                                self.assertEqual((restored / 'sub/a' if tree else restored).read_bytes(), b'verified bytes')
                            if force:
                                originals = ([old_out / name] if (old_out / name).is_file() and (old_out / name).read_bytes() == b'original' else list(old_out.glob('.dropin-aside-*/' + name)))
                                self.assertEqual(len(originals), 1)
                                self.assertEqual(originals[0].read_bytes(), b'original')
                                if originals[0].parent != old_out:
                                    self.assertIn('.dropin-aside-', str(caught.exception))

    def test_stage_replacement_does_not_clean_or_publish_unknown_directory(self):
        _, _, _, archive = self.archived('tree', tree=True)
        dump = self.engine.dump
        moved = self.out / 'owned-moved'
        @contextlib.contextmanager
        def replacing(*args, **kwargs):
            [stage] = self.out.glob('.dropin-restore-*')
            stage.rename(moved)
            stage.mkdir()
            (stage / 'keep').write_bytes(b'unknown')
            with dump(*args, **kwargs) as stream:
                yield stream
        with patch.object(self.engine, 'dump', replacing), self.assertRaises(module.RetrieveError):
            self.restore(archive)
        [impostor] = self.out.glob('.dropin-restore-*')
        self.assertEqual((impostor / 'keep').read_bytes(), b'unknown')
        self.assertFalse((self.out / 'tree').exists())
        self.assertTrue(moved.is_dir())

    def test_stage_root_replacement_at_publication_never_reports_verified(self):
        for phase in ('artifact-check', 'native-before'):
            for force in (False, True):
                with self.subTest(phase=phase, force=force):
                    name = f'tree-{phase}-{force}'
                    _, _, _, archive = self.archived(name, tree=True)
                    self.out = self.root / ('out-' + name)
                    self.out.mkdir()
                    target = self.out / name
                    if force:
                        target.write_bytes(b'original')
                    moved = self.out / 'owned-moved'
                    swapped = False
                    native = module.rename_noreplace
                    check = module.Destination.check_artifacts
                    def swap():
                        nonlocal swapped
                        if swapped:
                            return
                        swapped = True
                        [stage] = self.out.glob('.dropin-restore-*')
                        stage.rename(moved)
                        stage.mkdir()
                        (stage / 'keep').write_bytes(b'concurrent')
                    def checking(destination):
                        if phase == 'artifact-check':
                            swap()
                        return check(destination)
                    def renaming(sfd, source, dfd, destination):
                        if phase == 'native-before' and source.startswith('.dropin-restore-'):
                            swap()
                        return native(sfd, source, dfd, destination)
                    with patch.object(module.Destination, 'check_artifacts', checking), \
                         patch.object(module, 'rename_noreplace', renaming), \
                         self.assertRaises(module.RetrieveError) as caught:
                        self.restore(archive, force=force)
                    self.assertTrue(swapped)
                    self.assertEqual(caught.exception.kind, 'refused')
                    self.assertIn('identity', str(caught.exception))
                    self.assertEqual((moved / 'sub/a').read_bytes(), b'verified bytes')
                    impostors = list(self.out.glob('.dropin-restore-*/keep'))
                    if target.is_dir():
                        impostors.append(target / 'keep')
                    self.assertEqual(len(impostors), 1)
                    self.assertEqual(impostors[0].read_bytes(), b'concurrent')
                    if force:
                        originals = ([target] if target.is_file() else
                                     list(self.out.glob('.dropin-aside-*/' + name)))
                        self.assertEqual(len(originals), 1)
                        self.assertEqual(originals[0].read_bytes(), b'original')

    def test_ambiguous_publication_is_not_adopted_by_retry_or_written_to_archive(self):
        _, _, _, archive = self.archived()
        database_before = list(self.db.iterdump())
        repository_before = repr(self.engine._snapshots)
        native = module.rename_noreplace

        def late_error(*args):
            native(*args)
            raise OSError('publication result interrupted')

        with patch.object(module, 'rename_noreplace', late_error), \
             self.assertRaises(module.RetrieveError) as first:
            self.restore(archive)
        self.assertEqual(first.exception.kind, 'refused')
        self.assertIn('uncertain', str(first.exception))
        target = self.out / 'file.txt'
        self.assertEqual(target.read_bytes(), b'verified bytes')
        self.assertEqual(list(self.db.iterdump()), database_before)
        self.assertEqual(repr(self.engine._snapshots), repository_before)

        with self.assertRaises(module.RetrieveError) as retry:
            self.restore(archive)
        self.assertEqual(retry.exception.kind, 'refused')
        self.assertIn('exist', str(retry.exception).lower())
        self.assertEqual(target.read_bytes(), b'verified bytes')
        self.assertEqual(list(self.out.glob('.dropin-restore-*')), [])
        self.assertEqual(list(self.out.glob('.dropin-aside-*')), [])
        self.assertEqual(list(self.db.iterdump()), database_before)
        self.assertEqual(repr(self.engine._snapshots), repository_before)

    def test_native_error_after_success_retains_published_tree_without_chmod(self):
        _, _, _, archive = self.archived('tree', tree=True, captured_dir_mode=0o40555)
        native = module.rename_noreplace
        def late_error(*args):
            native(*args)
            raise OSError('native result interrupted')
        with patch.object(module, 'rename_noreplace', late_error), self.assertRaises(module.RetrieveError) as caught:
            self.restore(archive)
        target = self.out / 'tree'
        self.assertIn('retained', str(caught.exception))
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o555)
        self.assertEqual((target / 'sub/a').read_bytes(), b'verified bytes')
        target.chmod(0o700)
        (target / 'sub').chmod(0o700)
        (target / 'empty').chmod(0o700)

    def test_cleanup_does_not_follow_replaced_stage_symlink(self):
        _, _, _, archive = self.archived()
        outside = self.root / 'outside'
        outside.mkdir(mode=0o755)
        (outside / 'keep').write_bytes(b'untouched')
        dump = self.engine.dump
        @contextlib.contextmanager
        def replacing(*args, **kwargs):
            [stage] = self.out.glob('.dropin-restore-*')
            stage.rename(self.out / 'owned-moved')
            stage.symlink_to(outside, target_is_directory=True)
            with dump(*args, **kwargs) as stream:
                yield stream
        with patch.object(self.engine, 'dump', replacing), self.assertRaises(module.RetrieveError):
            self.restore(archive)
        self.assertEqual((outside / 'keep').read_bytes(), b'untouched')
        self.assertEqual(stat.S_IMODE(outside.stat().st_mode), 0o755)
        self.assertEqual(list(outside.iterdir()), [outside / 'keep'])

    def test_published_artifact_replacement_is_failure_without_removing_concurrent_data(self):
        for tree in (False, True):
            for force in (False, True):
                with self.subTest(tree=tree, force=force):
                    name = f'item-{tree}-{force}'
                    _, _, _, archive = self.archived(name, tree=tree)
                    target = self.out / name
                    if force:
                        target.write_bytes(b'original')
                    moved = self.out / (name + '-moved')
                    native = module.rename_noreplace
                    calls = 0
                    def replace(*args):
                        nonlocal calls
                        calls += 1
                        native(*args)
                        if calls == (2 if force else 1):
                            target.rename(moved)
                            target.write_bytes(b'concurrent')
                    with patch.object(module, 'rename_noreplace', replace), self.assertRaises(module.RetrieveError) as caught:
                        self.restore(archive, force=force)
                    self.assertIn('uncertain', str(caught.exception))
                    self.assertEqual(target.read_bytes(), b'concurrent')
                    self.assertEqual((moved / 'sub/a' if tree else moved).read_bytes(), b'verified bytes')
                    self.assertEqual(list(self.out.glob('.dropin-restore-*')), [])
                    if force:
                        [aside] = self.out.glob('.dropin-aside-*/' + name)
                        self.assertEqual(aside.read_bytes(), b'original')

    def test_sqlite_member_index_is_temporary_with_modest_cache_not_memory_database(self):
        _, _, _, archive = self.archived('tree', tree=True)
        connect = module.sqlite3.connect
        configurations = []
        def temporary(database, *args, **kwargs):
            self.assertEqual(database, '')
            db = connect(database, *args, **kwargs)
            db.set_trace_callback(configurations.append)
            self.assertEqual(db.execute('PRAGMA database_list').fetchone()[2], '')
            return db
        with patch.object(module.sqlite3, 'connect', side_effect=temporary) as mocked:
            self.restore(archive)
        self.assertEqual(mocked.call_count, 1)
        self.assertIn('PRAGMA cache_size=-2048', configurations)
        self.assertIn('PRAGMA temp_store=FILE', configurations)

    def test_unsupported_cleanup_chmod_is_reported_without_follow_fallback(self):
        _, _, _, archive = self.archived('tree', tree=True, captured_dir_mode=0o40000)
        native = module.rename_noreplace
        chmod = os.chmod
        def collision(*args):
            (self.out / 'tree').mkdir()
            return native(*args)
        def unsupported(path, mode, **kwargs):
            if mode == 0o700 and 'dir_fd' in kwargs:
                raise NotImplementedError('no safe chmod')
            return chmod(path, mode, **kwargs)
        with patch.object(module, 'rename_noreplace', collision), patch.object(os, 'chmod', unsupported), self.assertRaises(module.RetrieveError) as caught:
            self.restore(archive)
        self.assertEqual(caught.exception.kind, 'refused')
        self.assertIn('exist', str(caught.exception).lower())
        self.assertIn('cleanup failed', str(caught.exception))
        self.assertIn('no safe chmod', str(caught.exception))

    def test_aside_cleanup_failure_does_not_mask_primary_move_error(self):
        _, _, _, archive = self.archived()
        target = self.out / 'file.txt'
        target.write_bytes(b'original')
        rmdir = os.rmdir
        def denied(name, **kwargs):
            if str(name).startswith('.dropin-aside-'):
                raise PermissionError('aside cleanup denied')
            return rmdir(name, **kwargs)
        with patch.object(module, 'rename_noreplace', side_effect=OSError('primary aside move denied')), patch.object(os, 'rmdir', denied), self.assertRaises(module.RetrieveError) as caught:
            self.restore(archive, force=True)
        self.assertIn('primary aside move denied', str(caught.exception))
        self.assertIn('aside cleanup denied', str(caught.exception))
        self.assertIn('.dropin-aside-', str(caught.exception))
        self.assertEqual(target.read_bytes(), b'original')
        self.assertEqual(list(self.out.glob('.dropin-restore-*')), [])

    def test_aside_container_replacement_retains_original_and_unknown_data(self):
        _, _, _, archive = self.archived()
        target = self.out / 'file.txt'
        target.write_bytes(b'original')
        native = module.rename_noreplace
        moved = self.out / 'aside-moved'
        calls = 0
        def replace(*args):
            nonlocal calls
            calls += 1
            result = native(*args)
            if calls == 2:
                [aside] = self.out.glob('.dropin-aside-*')
                aside.rename(moved)
                aside.mkdir()
                (aside / 'keep').write_bytes(b'concurrent')
            return result
        with patch.object(module, 'rename_noreplace', replace), self.assertRaises(module.RetrieveError) as caught:
            self.restore(archive, force=True)
        self.assertIn('uncertain', str(caught.exception))
        self.assertIn('.dropin-aside-', str(caught.exception))
        self.assertEqual((moved / 'file.txt').read_bytes(), b'original')
        [aside] = self.out.glob('.dropin-aside-*')
        self.assertEqual((aside / 'keep').read_bytes(), b'concurrent')
        self.assertEqual(target.read_bytes(), b'verified bytes')
        self.assertEqual(list(self.out.glob('.dropin-restore-*')), [])

    def test_cleanup_error_report_survives_revoked_destination_access(self):
        _, _, _, archive = self.archived()
        def denied(stage):
            self.out.chmod(0)
            raise PermissionError('primary cleanup denied')
        try:
            with patch.object(module, '_cleanup', denied), self.assertRaises(module.RetrieveError) as caught:
                self.restore(archive)
            self.assertIn('primary cleanup denied', str(caught.exception))
            self.assertIn('uncertain', str(caught.exception))
        finally:
            self.out.chmod(0o700)
        self.assertEqual((self.out / 'file.txt').read_bytes(), b'verified bytes')

    def test_force_aside_target_identity_change_is_reported_not_silent_success(self):
        _, _, _, archive = self.archived()
        target = self.out / 'file.txt'
        target.write_bytes(b'original')
        original_moved = self.out / 'original-moved'
        native = module.rename_noreplace
        calls = 0
        def replace(*args):
            nonlocal calls
            calls += 1
            if calls == 1:
                target.rename(original_moved)
                target.write_bytes(b'concurrent')
            return native(*args)
        with patch.object(module, 'rename_noreplace', replace), self.assertRaises(module.RetrieveError) as caught:
            self.restore(archive, force=True)
        self.assertIn('changed', str(caught.exception))
        [aside] = self.out.glob('.dropin-aside-*/file.txt')
        self.assertIn(str(aside), str(caught.exception))
        self.assertEqual(aside.read_bytes(), b'concurrent')
        self.assertEqual(original_moved.read_bytes(), b'original')
        self.assertFalse(target.exists())
        self.assertEqual(list(self.out.glob('.dropin-restore-*')), [])
