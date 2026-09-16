"""Acquisition guard: creation provenance, lost events, and no unsafe fallback."""
import ctypes
import errno
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch


@unittest.skipUnless(sys.platform == 'linux', 'inotify acquisition guard is Linux-only')
class AcquisitionGuardTest(unittest.TestCase):
    def setUp(self):
        from dropin import retrieve_acquire
        self.module = retrieve_acquire
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, self.fd)

    def _event(self, wd, mask, name=b''):
        name = name + b'\0' if name else b''
        return struct.pack('=iIII', wd, mask, 0, len(name)) + name

    def test_unchanged_creation_is_accepted_and_watch_is_closed(self):
        with self.module.AcquisitionGuard(self.fd, 'stage') as guard:
            watch_fd = guard.fd
            os.mkdir('stage', 0o700, dir_fd=self.fd)
            guard.verify()
        with self.assertRaises(OSError):
            os.fstat(watch_fd)
        self.assertEqual(list((self.root / 'stage').iterdir()), [])

    def test_empty_renamed_in_replacement_is_not_accepted(self):
        (self.root / 'replacement').mkdir(mode=0o700)
        with self.module.AcquisitionGuard(self.fd, 'stage') as guard:
            os.mkdir('stage', 0o700, dir_fd=self.fd)
            (self.root / 'stage').rename(self.root / 'original')
            (self.root / 'replacement').rename(self.root / 'stage')
            with self.assertRaisesRegex(OSError, 'acquisition'):
                guard.verify()
        self.assertTrue((self.root / 'original').is_dir())
        self.assertTrue((self.root / 'stage').is_dir())

    def test_remove_and_recreate_or_rename_back_is_refused(self):
        for rename_back in (False, True):
            with self.subTest(rename_back=rename_back):
                name = 'stage-' + str(rename_back)
                with self.module.AcquisitionGuard(self.fd, name) as guard:
                    os.mkdir(name, 0o700, dir_fd=self.fd)
                    if rename_back:
                        (self.root / name).rename(self.root / 'moved')
                        (self.root / 'moved').rename(self.root / name)
                    else:
                        os.rmdir(name, dir_fd=self.fd)
                        os.mkdir(name, 0o700, dir_fd=self.fd)
                    with self.assertRaisesRegex(OSError, 'acquisition'):
                        guard.verify()
                self.assertTrue((self.root / name).is_dir())

    def test_substitution_by_another_process_is_refused(self):
        with self.module.AcquisitionGuard(self.fd, 'stage') as guard:
            os.mkdir('stage', 0o700, dir_fd=self.fd)
            subprocess.run([sys.executable, '-c',
                "from pathlib import Path; import sys; p=Path(sys.argv[1]); "
                "(p/'stage').rename(p/'original'); (p/'stage').mkdir(mode=0o700)",
                str(self.root)], check=True)
            with self.assertRaisesRegex(OSError, 'acquisition'):
                guard.verify()
        self.assertEqual(list((self.root / 'stage').iterdir()), [])
        self.assertEqual(list((self.root / 'original').iterdir()), [])

    def test_other_names_do_not_invalidate_unchanged_candidate(self):
        with self.module.AcquisitionGuard(self.fd, 'stage') as guard:
            os.mkdir('stage', 0o700, dir_fd=self.fd)
            (self.root / 'other').mkdir()
            (self.root / 'other').rename(self.root / 'other-moved')
            guard.verify()

    def test_missing_creation_overflow_watch_loss_and_malformed_events_refuse(self):
        with self.module.AcquisitionGuard(self.fd, 'stage') as guard:
            os.mkdir('stage', 0o700, dir_fd=self.fd)
            create = self._event(guard.wd, 0x40000100, b'stage')
            for invalid in (b'', self._event(-1, 0x4000),
                            self._event(guard.wd, 0x8000),
                            self._event(guard.wd, 0x2000),
                            create + b'bad',
                            create + self._event(guard.wd, 0x40000100, b'stage')):
                with self.subTest(invalid=invalid):
                    reads = [invalid, BlockingIOError(errno.EAGAIN, 'empty')]
                    with patch.object(self.module.os, 'read', side_effect=reads), \
                         self.assertRaisesRegex(OSError, 'acquisition'):
                        guard.verify()

    def test_read_failure_and_unbounded_event_stream_refuse(self):
        with self.module.AcquisitionGuard(self.fd, 'stage') as guard:
            os.mkdir('stage', 0o700, dir_fd=self.fd)
            with patch.object(self.module.os, 'read', side_effect=OSError('read failed')), \
                 self.assertRaises(OSError):
                guard.verify()
            irrelevant = self._event(guard.wd, 0x40000100, b'other')
            with patch.object(self.module.os, 'read', return_value=irrelevant) as read, \
                 self.assertRaisesRegex(OSError, 'acquisition'):
                guard.verify()
            self.assertLessEqual(read.call_count, 32)

    def test_verify_takes_eexist_parent_lock_barrier_before_reading(self):
        with self.module.AcquisitionGuard(self.fd, 'stage') as guard:
            os.mkdir('stage', 0o700, dir_fd=self.fd)
            operations = []
            mkdir, read = os.mkdir, os.read
            def creating(*args, **kwargs):
                operations.append('barrier')
                return mkdir(*args, **kwargs)
            def reading(*args):
                operations.append('read')
                return read(*args)
            with patch.object(self.module.os, 'mkdir', creating), \
                 patch.object(self.module.os, 'read', reading):
                guard.verify()
            self.assertEqual(operations[0], 'barrier')
            self.assertIn('read', operations)

    def test_barrier_failure_does_not_remove_or_chmod_anything(self):
        with self.module.AcquisitionGuard(self.fd, 'stage') as guard:
            os.mkdir('stage', 0o700, dir_fd=self.fd)
            with patch.object(self.module.os, 'mkdir', side_effect=PermissionError('barrier denied')), \
                 self.assertRaises(OSError):
                guard.verify()
        self.assertEqual((self.root / 'stage').stat().st_mode & 0o777, 0o700)

    def test_unsupported_platform_or_filesystem_refuses_without_artifacts(self):
        with patch.object(self.module.sys, 'platform', 'freebsd'), \
             self.assertRaises(OSError):
            with self.module.AcquisitionGuard(self.fd, 'stage'):
                self.fail('unsupported guard entered')
        with patch.object(self.module.sys, 'platform', 'linux'), \
             patch.object(self.module, '_filesystem_type', return_value=0x6969), \
             self.assertRaises(OSError):
            with self.module.AcquisitionGuard(self.fd, 'stage'):
                self.fail('network filesystem guard entered')
        self.assertEqual(list(self.root.iterdir()), [])

    def test_unverified_lp64_architecture_refuses_before_any_native_call(self):
        for machine in ('mips64', 'ppc64', 'unknown'):
            with self.subTest(machine=machine):
                with patch('platform.machine', return_value=machine), \
                     patch.object(self.module, '_filesystem_type', return_value=0xEF53) as native, \
                     patch.object(self.module.ctypes, 'CDLL') as library, \
                     self.assertRaises(OSError):
                    with self.module.AcquisitionGuard(self.fd, 'stage'):
                        pass
                native.assert_not_called()
                library.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_non_lp64_widths_refuse_before_any_native_call(self):
        for short_type in (ctypes.c_long, ctypes.c_void_p):
            def size(kind):
                return 4 if kind is short_type else 8
            with self.subTest(short_type=short_type):
                with patch('platform.machine', return_value='aarch64'), \
                     patch.object(self.module.ctypes, 'sizeof', side_effect=size), \
                     patch.object(self.module, '_filesystem_type') as native, \
                     patch.object(self.module.ctypes, 'CDLL') as library, \
                     self.assertRaises(OSError):
                    with self.module.AcquisitionGuard(self.fd, 'stage'):
                        pass
                native.assert_not_called()
                library.assert_not_called()

    def test_native_watch_failure_closes_fd_and_preserves_errno(self):
        fd = os.open('/dev/null', os.O_RDONLY)
        init = Mock(return_value=fd)
        add = Mock(return_value=-1)
        library = Mock(inotify_init1=init, inotify_add_watch=add)
        with patch.object(self.module, '_filesystem_type', return_value=0xEF53), \
             patch.object(self.module.ctypes, 'CDLL', return_value=library), \
             patch.object(self.module.ctypes, 'get_errno', return_value=errno.EACCES), \
             self.assertRaises(OSError) as caught:
            with self.module.AcquisitionGuard(self.fd, 'stage'):
                self.fail('failed watch entered')
        self.assertEqual(caught.exception.errno, errno.EACCES)
        with self.assertRaises(OSError):
            os.fstat(fd)
        init.assert_called_once_with(os.O_NONBLOCK | os.O_CLOEXEC)
        add.assert_called_once_with(fd, os.fsencode(f'/proc/self/fd/{self.fd}'), self.module.WATCH_MASK)
        self.assertEqual(init.argtypes, [ctypes.c_int])
        self.assertEqual(add.argtypes, [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32])

    def test_missing_native_symbol_refuses_without_artifacts(self):
        with patch.object(self.module.ctypes, 'CDLL', return_value=object()), \
             self.assertRaises(OSError):
            with self.module.AcquisitionGuard(self.fd, 'stage'):
                self.fail('missing inotify entered')
        self.assertEqual(list(self.root.iterdir()), [])


class DarwinAcquisitionTest(unittest.TestCase):
    def test_creation_barrier_runs_without_linux_watcher(self):
        from dropin import retrieve_acquire as module
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with patch.object(module.sys, 'platform', 'darwin'), \
                     module.AcquisitionGuard(fd, 'stage') as guard:
                    self.assertIsNone(guard.fd)
                    os.mkdir('stage', 0o700, dir_fd=fd)
                    guard.verify()
                self.assertEqual(list((root / 'stage').iterdir()), [])
            finally:
                os.close(fd)
