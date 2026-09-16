"""Fixture-backed macOS fake.

Configured explicitly or not at all: an unconfigured path is a `CaptureFailure`,
and ownership is `unsupported` until a test says otherwise. Silence would let a
test pass because a check never ran.
"""

from __future__ import annotations

from pathlib import Path
import plistlib

from ..capture.mdimport_parser import parse_mdimport
from ..capture.mdls_parser import Attr, parse_mdls
from .interface import (COMMENT_XATTR, TAGS_XATTR, Capabilities, CaptureFailure,
                        LaunchAgent, Unsupported, XattrValue, is_bundle_tree)


class FakeMacOS:
    validation_state = "fake"

    def __init__(self) -> None:
        self._mdls: dict[str, str] = {}
        self._importer: dict[str, str] = {}
        self._importer_failures: dict[str, str] = {}
        self._xattrs: dict[str, dict[str, XattrValue]] = {}
        self._ownership_supported = False
        self._ownership_reason = "not configured in this test"
        self._holders: dict[tuple[str, bool], list[int]] = {}
        self.launch_agents: list[LaunchAgent] = []

    # ---- configuration -----------------------------------------------------

    def set_mdls(self, path: str, text: str) -> None:
        self._mdls[path] = text

    def set_importer(self, path: str, text: str) -> None:
        self._importer[path] = text

    def fail_importer(self, path: str, reason: str) -> None:
        self._importer_failures[path] = reason

    def set_xattr(self, path: str, name: str, value: bytes) -> None:
        self._xattrs.setdefault(path, {})[name] = XattrValue(value, "ok")

    def make_xattr_unreadable(self, path: str, name: str, reason: str) -> None:
        self._xattrs.setdefault(path, {})[name] = XattrValue(None, f"skipped:{reason}")

    def set_ownership_supported(self, supported: bool, reason: str = "") -> None:
        self._ownership_supported = supported
        self._ownership_reason = reason or ("" if supported else "disabled by test")

    def set_open_holder(self, path: str, pid: int, is_dir: bool = False) -> None:
        self._holders.setdefault((path, is_dir), []).append(pid)

    # ---- seam --------------------------------------------------------------

    def mdls(self, path: str) -> dict[str, Attr]:
        if path not in self._mdls:
            raise CaptureFailure(f"no mdls fixture configured for {path}")
        return parse_mdls(self._mdls[path])

    def importer_attributes(self, path: str) -> dict[str, Attr]:
        if path in self._importer_failures:
            raise CaptureFailure(self._importer_failures[path])
        if path not in self._importer:
            raise CaptureFailure(f"no importer fixture configured for {path}")
        return parse_mdimport(self._importer[path])

    def xattrs(self, path: str) -> dict[str, XattrValue]:
        return dict(self._xattrs.get(path, {}))

    def finder_tags(self, path: str) -> list[str]:
        raw = self._plist_xattr(path, TAGS_XATTR)
        if raw is None:
            return []
        # Finder stores "<tag>\n<colour index>"; the colour is presentation.
        return [str(entry).split("\n", 1)[0] for entry in raw]

    def finder_comment(self, path: str) -> str | None:
        raw = self._plist_xattr(path, COMMENT_XATTR)
        return None if raw is None else str(raw)

    def _plist_xattr(self, path: str, name: str):
        entry = self._xattrs.get(path, {}).get(name)
        if entry is None or entry.value is None:
            return None
        try:
            return plistlib.loads(entry.value)
        except Exception as error:  # plistlib raises several unrelated types
            raise CaptureFailure(f"{name} on {path} is not a property list: "
                                 f"{error}") from error

    def is_bundle(self, path: str) -> bool:
        tree = self.mdls(path).get("kMDItemContentTypeTree")
        return is_bundle_tree(tree.value if tree else ())

    def open_descriptors(self, path: str, is_dir: bool) -> list[int]:
        if not self._ownership_supported:
            raise Unsupported(self._ownership_reason)
        return list(self._holders.get((path, is_dir), []))

    def capabilities(self) -> Capabilities:
        return Capabilities(ownership_check=self._ownership_supported,
                            ownership_reason=self._ownership_reason,
                            scope_note="fake: whatever the test configured")

    def write_launch_agent(self, path: Path, *, label: str, program,
                           queue_directories, interval: int) -> None:
        self.launch_agents.append(LaunchAgent(
            path=Path(path), label=label, program=tuple(program),
            queue_directories=tuple(queue_directories), interval=interval))
