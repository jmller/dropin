"""Atomic no-replace publication seam, Linux native and mocked Darwin ABI."""
import ctypes
import errno
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch


class RenameTest(unittest.TestCase):
    @unittest.skipUnless(sys.platform == 'linux', 'real Linux renameat2 only')
    def test_real_linux_never_replaces_existing_file_directory_or_symlink(self):
        from dropin.retrieve_rename import rename_noreplace
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            self.addCleanup(os.close, fd)
            for kind in ('file', 'dir', 'symlink'):
                src, dst = root / 'src', root / 'dst'
                src.write_bytes(b'new')
                if kind == 'dir': dst.mkdir()
                elif kind == 'symlink': dst.symlink_to('absent')
                else: dst.write_bytes(b'old')
                with self.assertRaises(FileExistsError):
                    rename_noreplace(fd, 'src', fd, 'dst')
                self.assertEqual(src.read_bytes(), b'new')
                if kind == 'dir': dst.rmdir()
                else: dst.unlink()
                rename_noreplace(fd, 'src', fd, 'dst')
                self.assertEqual(dst.read_bytes(), b'new')
                dst.unlink()

    def test_native_abi_flags_errno_and_unsupported_fail_closed(self):
        from dropin import retrieve_rename as module
        for platform, symbol, flag in [('linux', 'renameat2', 1), ('darwin', 'renameatx_np', 4)]:
            function = Mock(return_value=0)
            library = Mock(**{symbol: function})
            with self.subTest(platform=platform), patch.object(module.sys, 'platform', platform), patch.object(module.ctypes, 'CDLL', return_value=library):
                module.rename_noreplace(3, 'a', 4, 'b')
                function.assert_called_once_with(3, b'a', 4, b'b', flag)
                self.assertEqual(function.argtypes, [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint])
                self.assertEqual(function.restype, ctypes.c_int)
                function.return_value = -1
                with patch.object(module.ctypes, 'get_errno', return_value=errno.EEXIST), self.assertRaises(FileExistsError):
                    module.rename_noreplace(3, 'a', 4, 'b')
        with patch.object(module.sys, 'platform', 'unsupported'), self.assertRaises(OSError):
            module.rename_noreplace(3, 'a', 4, 'b')
        with patch.object(module.ctypes, 'CDLL', return_value=object()), self.assertRaises(OSError):
            module.rename_noreplace(3, 'a', 4, 'b')
