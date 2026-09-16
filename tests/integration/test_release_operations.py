"""Release operation lifecycle: launchd, writer lock, and repository lock."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from dropin import launchd
from dropin.config import load
from dropin.engine.interface import EngineError
from dropin.engine.restic import ResticEngine
from dropin.macos.real import RealMacOS
from dropin.pipeline.writer_lock import LockHeld, writer_lock
from tests.unit.test_config import EXAMPLE


class ReleaseOperationsTest(unittest.TestCase):
    def test_launchd_install_bootout_and_plist_removal_preserve_state(self):
        with tempfile.TemporaryDirectory(prefix="dropin-operations-") as raw:
            root = Path(raw)
            config, state, password = root / "config.toml", root / "state", root / "password"
            state.mkdir()
            config.write_bytes(b"config sentinel")
            (state / "store.sqlite").write_bytes(b"catalog sentinel")
            password.write_bytes(b"password sentinel")
            before = {path: path.read_bytes()
                      for path in (config, state / "store.sqlite", password)}
            agent_path = root / "Library/LaunchAgents/dev.dropin.drain.plist"
            agent = launchd.install(
                RealMacOS(platform="darwin"), config_path=config, drop_dir=root / "Drop",
                label="dev.dropin.drain", interval=900, agent_path=agent_path)
            self.assertEqual(
                launchd.bootstrap_command(agent.path, uid=501),
                f"launchctl bootstrap gui/501 {agent_path}")
            self.assertEqual(
                launchd.bootout_command(agent.path, uid=501),
                f"launchctl bootout gui/501 {agent_path}")
            self.assertTrue(agent_path.is_file())
            agent_path.unlink()  # Removal is intentionally an explicit operator action.
            self.assertFalse(agent_path.exists())
            self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_second_writer_is_refused_until_holder_exits(self):
        with tempfile.TemporaryDirectory(prefix="dropin-operations-") as raw:
            lock = Path(raw) / "writer.lock"
            script = (
                "import sys,time; from dropin.pipeline.writer_lock import writer_lock; "
                "p=sys.argv[1]; "
                "ctx=writer_lock(p,verb='drain'); ctx.__enter__(); "
                "print('ready',flush=True); time.sleep(30)"
            )
            holder = subprocess.Popen(
                [sys.executable, "-c", script, str(lock)], cwd=Path(__file__).parents[2],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.addCleanup(holder.kill)
            self.assertEqual(holder.stdout.readline().strip(), "ready")
            with self.assertRaises(LockHeld):
                with writer_lock(lock, verb="recover"):
                    pass
            holder.terminate()
            holder.communicate(timeout=10)
            with writer_lock(lock, verb="recover"):
                pass

    def test_repository_lock_is_actionable_and_does_not_remove_source(self):
        temporary = tempfile.TemporaryDirectory(prefix="dropin-operations-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        drop, state = root / "drop", root / "state"
        drop.mkdir()
        state.mkdir()
        password = root / "password"
        password.write_text("disposable")
        password.chmod(0o600)
        config_path = root / "config.toml"
        config_path.write_text(EXAMPLE.format(
            drop=drop, state=state, password=password))
        source = drop / "queued.txt"
        source.write_bytes(b"queued")
        engine = ResticEngine(load(config_path))
        completed = subprocess.CompletedProcess(
            [], 11, b"", b"repository is already locked")
        with mock.patch("subprocess.run", return_value=completed), \
             self.assertRaises(EngineError) as caught:
            engine.snapshots()
        self.assertEqual(caught.exception.kind, "locked")
        self.assertEqual(source.read_bytes(), b"queued")


if __name__ == "__main__":
    unittest.main()
