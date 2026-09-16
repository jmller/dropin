"""The real macOS adapter: subprocess only, thin by design.

Validated against the complete real-output recordings for `mdls`,
`mdimport`, `xattr`, and `lsof`. This declaration covers adapter command parsing
and ownership positive/negative controls only; live macOS retrieval and
release approval remain separate live gates.

The ownership rules are the load-bearing part. "Clear" means exactly: exit 0 or
1, no `p` records, empty stderr. Anything else — a warning, an unexpected
status, unparseable output, a failed self-open control — is `Unsupported`, and
eviction stops. An empty result from a check we cannot trust is not evidence of
absence.
"""

from __future__ import annotations

import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import tempfile

from ..capture.mdimport_parser import MdimportParseError, parse_mdimport
from ..capture.mdls_parser import Attr, MdlsParseError, parse_mdls
from .interface import (COMMENT_XATTR, TAGS_XATTR, Capabilities, CaptureFailure,
                        LaunchAgent, Unsupported, XattrValue, is_bundle_tree)

SCOPE_NOTE = ("visible to the invoking user only; a non-root check cannot see "
              "other users' processes, and mmap-only access is not covered")



class RealMacOS:
    # Adapter validation is complete from the real-output evidence set; the
    # live macOS retrieval/eviction release gate is separate.
    validation_state = "validated"

    def __init__(self, lsof: str = "lsof", timeout: int = 60,
                 platform: str | None = None) -> None:
        self.lsof = lsof
        self.timeout = timeout
        # Injectable so the ownership logic is testable on Linux; the default
        # still refuses to claim the capability anywhere but macOS.
        self.platform = platform if platform is not None else sys.platform

    # ---- metadata ----------------------------------------------------------

    def mdls(self, path: str) -> dict[str, Attr]:
        try:
            return parse_mdls(self._capture(["mdls", path], "mdls"))
        except MdlsParseError as error:
            raise CaptureFailure(f"mdls on {path}: {error}") from error

    def importer_attributes(self, path: str) -> dict[str, Attr]:
        try:
            return parse_mdimport(
                self._capture(["mdimport", "-d3", "-n", path], "mdimport"))
        except MdimportParseError as error:
            raise CaptureFailure(f"mdimport on {path}: {error}") from error

    def _capture(self, argv: list[str], what: str) -> str:
        try:
            result = subprocess.run(argv, capture_output=True, timeout=self.timeout)
        except (OSError, subprocess.SubprocessError) as error:
            raise CaptureFailure(f"{what} failed: {error}") from error
        if result.returncode != 0:
            raise CaptureFailure(
                f"{what} exited {result.returncode}: "
                f"{result.stderr.decode(errors='replace').strip()}")
        # mdimport writes its diagnostics to stderr; mdls to stdout.
        text = result.stdout.decode(errors="surrogateescape")
        return text if text.strip() else result.stderr.decode(errors="surrogateescape")

    def xattrs(self, path: str) -> dict[str, XattrValue]:
        try:
            listing = subprocess.run(["xattr", path], capture_output=True,
                                     timeout=self.timeout)
        except (OSError, subprocess.SubprocessError) as error:
            raise CaptureFailure(f"xattr failed: {error}") from error
        entries: dict[str, XattrValue] = {}
        for name in listing.stdout.decode(errors="surrogateescape").split():
            result = subprocess.run(["xattr", "-p", "-x", name, path],
                                    capture_output=True, timeout=self.timeout)
            if result.returncode != 0:
                reason = result.stderr.decode(errors="replace").strip() or "unreadable"
                entries[name] = XattrValue(None, f"skipped:{reason}")
                continue
            try:
                entries[name] = XattrValue(
                    _decode_hex_dump(result.stdout.decode(errors="replace")), "ok")
            except ValueError:
                entries[name] = XattrValue(None, "skipped:undecodable hex dump")
        return entries

    def finder_tags(self, path: str) -> list[str]:
        raw = self._plist_xattr(path, TAGS_XATTR)
        if raw is None:
            return []
        return [str(entry).split("\n", 1)[0] for entry in raw]

    def finder_comment(self, path: str) -> str | None:
        raw = self._plist_xattr(path, COMMENT_XATTR)
        return None if raw is None else str(raw)

    def _plist_xattr(self, path: str, name: str):
        entry = self.xattrs(path).get(name)
        if entry is None or entry.value is None:
            return None
        try:
            return plistlib.loads(entry.value)
        except Exception as error:
            raise CaptureFailure(f"{name} on {path} is not a property list: "
                                 f"{error}") from error

    def is_bundle(self, path: str) -> bool:
        tree = self.mdls(path).get("kMDItemContentTypeTree")
        return is_bundle_tree(tree.value if tree else ())

    # ---- ownership ---------------------------------------------------------

    def open_descriptors(self, path: str, is_dir: bool) -> list[int]:
        return self._lsof(path, is_dir)

    def _argv(self, path: str, is_dir: bool) -> list[str]:
        # `+D` requires a directory; a single file needs the plain form.
        if is_dir:
            # +D consumes its next argument, so '--' cannot go between it
            # and the operand. Absolute paths cannot be mistaken for options.
            return [self.lsof, "-Fpn", "+D", os.path.abspath(path)]
        return [self.lsof, "-Fpn", "--", path]

    def _lsof(self, path: str, is_dir: bool) -> list[int]:
        argv = self._argv(path, is_dir)
        try:
            result = subprocess.run(argv, capture_output=True, timeout=self.timeout)
        except (OSError, subprocess.SubprocessError) as error:
            raise Unsupported(f"cannot run {self.lsof}: {error}") from error
        if result.returncode not in (0, 1):
            raise Unsupported(
                f"{self.lsof} exited {result.returncode}: "
                f"{result.stderr.decode(errors='replace').strip()}")
        if result.stderr.strip():
            # A warning means the scan may have skipped something; an empty
            # result then proves nothing.
            raise Unsupported(
                f"{self.lsof} wrote to stderr: "
                f"{result.stderr.decode(errors='replace').strip()}")
        return _parse_lsof(result.stdout.decode(errors="surrogateescape"))

    def capabilities(self) -> Capabilities:
        if self.platform != "darwin":
            return Capabilities(False, "the lsof adapter is macOS-only", SCOPE_NOTE)
        try:
            return self._probe_capability()
        except Unsupported as error:
            return Capabilities(False, str(error), SCOPE_NOTE)

    def _probe_capability(self) -> Capabilities:
        """Open a file ourselves, require both forms to see it, then require
        both to clear once it is closed."""
        pid = os.getpid()
        with tempfile.TemporaryDirectory(prefix="dropin-own-probe-") as temp:
            probe = Path(temp) / "probe"
            probe.write_bytes(b"probe")
            with probe.open("rb"):
                open_file = self._lsof(str(probe), is_dir=False)
                open_dir = self._lsof(temp, is_dir=True)
            if pid not in open_file or pid not in open_dir:
                return Capabilities(
                    False,
                    "positive control failed: our own open descriptor was not "
                    "visible, so an empty result cannot mean 'nobody has it open'",
                    SCOPE_NOTE)
            closed_file = self._lsof(str(probe), is_dir=False)
            closed_dir = self._lsof(temp, is_dir=True)
            if closed_file or closed_dir:
                return Capabilities(
                    False, "negative control failed: a closed file still reports "
                           "a holder", SCOPE_NOTE)
        return Capabilities(True, "", SCOPE_NOTE)

    # ---- launchd -----------------------------------------------------------

    def write_launch_agent(self, path: Path, *, label: str, program,
                           queue_directories, interval: int) -> None:
        from ..launchd import render_plist

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_plist(label=label, program=program,
                                     queue_directories=queue_directories,
                                     interval=interval))
        self.last_launch_agent = LaunchAgent(
            path=path, label=label, program=tuple(program),
            queue_directories=tuple(queue_directories), interval=interval)


def _decode_hex_dump(text: str) -> bytes:
    """Bytes out of `xattr -p -x` output.

    Provisional: the exact column layout differs between macOS releases,
    so this accepts both the bare hex stream and an offset/ASCII layout by
    keeping only two-digit hex tokens outside an `|ascii|` column.
    """
    collected: list[str] = []
    for line in text.splitlines():
        body = line.split("|", 1)[0]
        tokens = body.split()
        if tokens and len(tokens[0]) == 8 and all(
                char in "0123456789abcdefABCDEF" for char in tokens[0]):
            tokens = tokens[1:]  # leading offset column
        for token in tokens:
            if len(token) != 2 or any(
                    char not in "0123456789abcdefABCDEF" for char in token):
                raise ValueError(f"not a hex dump token: {token!r}")
            collected.append(token)
    if not collected:
        raise ValueError("empty hex dump")
    return bytes.fromhex("".join(collected))


def _parse_lsof(stdout: str) -> list[int]:
    """`-Fpn` also emits file-set markers (`f`) on macOS lsof 4.91.

    Orphan name/descriptor records cannot prove a clear scan. Unknown fields
    still refuse, as do malformed PIDs and descriptor markers.
    """
    pids: list[int] = []
    for line in stdout.splitlines():
        if not line:
            continue
        field, value = line[0], line[1:]
        if field == "p":
            if not re.fullmatch(r"[0-9]+", value) or int(value) <= 0:
                raise Unsupported(f"unparseable lsof pid record: {line!r}")
            pids.append(int(value))
        elif field in ("f", "n"):
            if not pids or not value:
                raise Unsupported(f"orphan or empty lsof field record: {line!r}")
            if field == "f" and not re.fullmatch(
                    r"[0-9]+|cwd|rtd|txt|mem|mmap|ltx|pd|err|NOFD", value):
                raise Unsupported(f"unparseable lsof descriptor record: {line!r}")
        else:
            raise Unsupported(f"unexpected lsof field record: {line!r}")
    return pids
