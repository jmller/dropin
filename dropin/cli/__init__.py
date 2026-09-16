"""What every verb shares: the run context, report rendering, the tool gate.

The seams are built lazily so a query verb never constructs an engine, and so
a contract test can select the in-memory engine fake without restic installed.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
import sys

from ..config import Config, ConfigError
from ..report import (ITEM_FAILURES, Outcome, Report, VERIFICATION_FAILURES,
                      diagnostic_secrets, redact_diagnostic)

#: Selects the in-memory engine fake for contract tests that have no restic.
#: Under it the ownership adapter is forced *unsupported*, so nothing can ever
#: be deleted on the strength of a repository that lives in process memory.
ENGINE_FAKE_ENV = "DROPIN_ENGINE_FAKE"


@dataclass
class Context:
    """What a verb gets: configuration plus lazily built seams."""

    config: Config
    json_output: bool = False
    nul_separated: bool = False
    _engine: object | None = None
    _macos: object | None = None
    _ownership: object | None = None
    _db: object | None = None

    @property
    def fake_engine(self) -> bool:
        return bool(os.environ.get(ENGINE_FAKE_ENV))

    @property
    def engine(self):
        if self._engine is None:
            if self.fake_engine:
                from ..engine.fake import FakeEngine

                self._engine = FakeEngine()
                self._engine.init()
            else:
                from ..engine.restic import ResticEngine

                self._engine = ResticEngine(self.config)
        return self._engine

    @property
    def db(self):
        """The store, opened on first use. Missing means `init` never ran."""
        if self._db is None:
            from ..store.db import connect

            if not self.config.store_path.exists():
                raise ConfigError(f"no store at {self.config.store_path}; "
                                  f"run `dropin init` first")
            self._db = connect(self.config.store_path)
        return self._db

    @property
    def macos(self):
        """The real adapter on macOS, the fixture-backed fake anywhere else."""
        if self._macos is None:
            if sys.platform == "darwin" and not os.environ.get("DROPIN_MACOS_FAKE"):
                from ..macos.real import RealMacOS

                self._macos = RealMacOS(lsof=self.config.lsof)
            else:
                from ..macos.fake import FakeMacOS

                self._macos = FakeMacOS()
        return self._macos

    @property
    def ownership(self):
        """Platform-appropriate open-descriptor adapter; fails closed elsewhere."""
        if self._ownership is None:
            if self.fake_engine:
                self._ownership = _NoOwnership("fake engine (eviction disabled)")
            elif sys.platform == "darwin":
                from ..macos.real import RealMacOS

                self._ownership = RealMacOS(lsof=self.config.lsof)
            elif sys.platform.startswith("linux"):
                from ..ownership.linux import LinuxOwnership

                self._ownership = LinuxOwnership()
            else:
                self._ownership = _NoOwnership(sys.platform)
        return self._ownership


class _NoOwnership:
    """No validated check on this platform, so eviction cannot proceed."""

    def __init__(self, platform: str) -> None:
        self.platform = platform

    def capabilities(self):
        from ..macos.interface import Capabilities

        return Capabilities(False, f"no ownership adapter for {self.platform}", "")

    def open_descriptors(self, path: str, is_dir: bool):
        from ..macos.interface import Unsupported

        raise Unsupported(f"no ownership adapter for {self.platform}")


def emit(context: Context, report: Report) -> int:
    """Render a report the documented way and return its exit code.

    NDJSON: every record on stdout. Human: one line per record; failures go to
    stderr so a quiet stdout means success. A whole-run refusal is always
    a stderr diagnostic.
    """
    secrets = diagnostic_secrets(context.config)
    if report.run_refusal:
        reason = redact_diagnostic(report.run_refusal, secrets)
        print(f"dropin: {report.verb} refused: {reason}", file=sys.stderr)
    if report.run_error:
        reason = redact_diagnostic(report.run_error, secrets)
        print(f"dropin: {report.verb} failed: {reason}", file=sys.stderr)
    loud = ITEM_FAILURES | VERIFICATION_FAILURES | {Outcome.ORPHANED}
    for record in report.records:
        if context.json_output:
            print(record.to_json(secrets))
        else:
            print(record.to_human(secrets),
                  file=sys.stderr if record.outcome in loud else sys.stdout)
    sys.stdout.flush()
    return report.exit_code()


def tools_gate(context: Context) -> str | None:
    """Pinned-tool check; None when it passes. Skipped under the engine fake."""
    if context.fake_engine:
        return None
    from ..engine.tools import ToolGateError, check_tools

    try:
        check_tools(context.config)
    except ToolGateError as error:
        return str(error)
    return None
