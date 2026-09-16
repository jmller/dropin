"""Shared test helpers.

Nothing here reaches the network, the real macOS seams, or a real repository.
`tools_available()` is the single gate the integration suite uses so a missing
binary reports *skipped* rather than passing vacuously.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
from typing import Callable, TypeVar

T = TypeVar("T")

RESTIC_ENV = "DROPIN_RESTIC_BIN"
RCLONE_ENV = "DROPIN_RCLONE_BIN"
FAULT_ENV = "DROPIN_FAULT_AFTER"


class TempStateDir:
    """A state directory laid out the way `init` lays one out.

    Use as a context manager or call `cleanup()`; `unittest` callers usually
    want `self.addCleanup(state.cleanup)`.
    """

    def __init__(self, prefix: str = "dropin-state-") -> None:
        self._temp = tempfile.TemporaryDirectory(prefix=prefix)
        self.root = Path(self._temp.name)
        self.state_dir = self.root / "state"
        self.drop_dir = self.root / "drop"
        for path in (self.state_dir, self.drop_dir):
            path.mkdir()
        for name in ("export", "cache", "tmp"):
            (self.state_dir / name).mkdir()

    @property
    def store_path(self) -> Path:
        return self.state_dir / "store.sqlite"

    @property
    def export_dir(self) -> Path:
        return self.state_dir / "export"

    @property
    def cache_dir(self) -> Path:
        return self.state_dir / "cache"

    @property
    def tmp_dir(self) -> Path:
        return self.state_dir / "tmp"

    def drop(self, name: str, content: bytes = b"") -> Path:
        """Place a file directly in the spool and return its path."""
        path = self.drop_dir / name
        path.write_bytes(content)
        return path

    def cleanup(self) -> None:
        self._temp.cleanup()

    def __enter__(self) -> "TempStateDir":
        return self

    def __exit__(self, *exc: object) -> None:
        self.cleanup()


def tools_available() -> tuple[str, str] | None:
    """Return (restic, rclone) paths when both pinned binaries are usable.

    Returns None instead of raising so a caller can `skipTest` with a reason.
    Resolution order is the explicit environment variable, then `PATH`.
    """
    restic = shutil.which(os.environ.get(RESTIC_ENV) or "restic")
    rclone = shutil.which(os.environ.get(RCLONE_ENV) or "rclone")
    if not restic or not rclone:
        return None
    for executable in (restic, rclone):
        try:
            subprocess.run([executable, "version"], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=30)
        except (OSError, subprocess.SubprocessError):
            return None
    return restic, rclone


def local_rclone_conf(path: Path) -> Path:
    """Write an rclone config with a single local backend named `local`."""
    path.write_text("[local]\ntype = local\n")
    return path


def run_cli(args: list[str], env: dict[str, str] | None = None,
            cwd: Path | None = None, stdin: bytes | None = None,
            timeout: float = 300) -> subprocess.CompletedProcess[bytes]:
    """Run `python3 -m dropin` as a real subprocess.

    Contract tests assert on argv, exit codes, and stream separation, so the CLI
    is exercised the way a user runs it rather than by calling main() in-process.
    """
    full_env = dict(os.environ)
    if env is not None:
        full_env.update(env)
    return subprocess.run(
        [sys.executable, "-m", "dropin", *args],
        input=stdin, capture_output=True, timeout=timeout,
        cwd=str(cwd) if cwd else None, env=full_env,
    )


class FaultHook:
    """Configures `DROPIN_FAULT_AFTER` for crash-injection tests.

    The pipeline reads the variable and aborts immediately after committing the
    named transition, so a re-run exercises the real resume path.
    """

    def __init__(self, after: str) -> None:
        self.after = after

    def env(self, base: dict[str, str] | None = None) -> dict[str, str]:
        env = dict(base or {})
        env[FAULT_ENV] = self.after
        return env

    def __enter__(self) -> "FaultHook":
        self._previous = os.environ.get(FAULT_ENV)
        os.environ[FAULT_ENV] = self.after
        return self

    def __exit__(self, *exc: object) -> None:
        if self._previous is None:
            os.environ.pop(FAULT_ENV, None)
        else:
            os.environ[FAULT_ENV] = self._previous


def mutate_during(callable_: Callable[[], T], mutation: Callable[[], None],
                  *, ready: threading.Event | None = None,
                  timeout: float = 30) -> T:
    """Run `mutation` on another thread while `callable_` is in flight.

    Used by the gate tests: a source change has to land *during* hashing, upload,
    or verification, not before or after, or the gate under test is not the one
    being exercised. When `ready` is given, the mutation waits for the pipeline
    to set it; otherwise it fires as soon as the thread is scheduled.
    """
    error: list[BaseException] = []

    def run_mutation() -> None:
        try:
            if ready is not None and not ready.wait(timeout):
                raise AssertionError("pipeline never signalled the mutation point")
            mutation()
        except BaseException as exc:  # surfaced to the caller below
            error.append(exc)

    thread = threading.Thread(target=run_mutation, name="mutate_during")
    thread.start()
    try:
        return callable_()
    finally:
        thread.join(timeout)
        if thread.is_alive():
            raise AssertionError("mutation thread did not finish")
        if error:
            raise error[0]
