"""The restic adapter: subprocess in, typed values out.

Thin on purpose, so the macOS/restic boundary stays a testable seam. Every
behaviour it relies on is demonstrated by `scripts/restic_contract_probe.py` and
covered by the repository tests. Two of those recordings shape this file:
`ls --json` nodes carry no link target, and the item path must be passed to
`ls` or ancestor directories appear.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import subprocess

from ..config import Config
from .interface import (BackupResult, Engine, EngineError, Identity, Node,
                        Snapshot, TagError, parse_tags)

__all__ = ["ResticEngine", "Identity", "TagError", "parse_tags", "Engine"]

STDERR_TAIL_LINES = 20

#: Documented restic exit codes. 3 is a backup outcome, not a failure kind.
EXIT_KINDS = {10: "no-repo", 11: "locked", 12: "bad-password"}


class ResticEngine:
    def __init__(self, config: Config) -> None:
        self.config = config

    # ---- invocation --------------------------------------------------------

    def _base(self) -> list[str]:
        return [
            self.config.restic,
            "--repo", self.config.repo,
            "--cache-dir", str(self.config.cache_dir),
            "--option", f"rclone.program={self.config.rclone}",
            "--option", f"rclone.connections={self.config.rclone_connections}",
        ]

    def _run(self, args: list[str], *, expect_json: bool = True,
             allow: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess:
        argv = self._base() + args
        try:
            result = subprocess.run(argv, capture_output=True,
                                    env=self.config.restic_env(),
                                    timeout=self.config.timeout_seconds)
        except subprocess.TimeoutExpired as error:
            raise EngineError("tool-error",
                              f"restic timed out after "
                              f"{self.config.timeout_seconds}s") from error
        except OSError as error:
            raise EngineError("tool-error", f"cannot run {self.config.restic}: "
                                            f"{error}") from error
        if result.returncode not in allow:
            raise self._error(result)
        return result

    def _error(self, result: subprocess.CompletedProcess, *,
               operation: str = "restic") -> EngineError:
        tail = _tail(result.stderr)
        kind = EXIT_KINDS.get(result.returncode, "tool-error")
        diagnostic = tail.lower()
        if ("repository version" in diagnostic
                and any(marker in diagnostic
                        for marker in ("too new", "not supported", "unsupported"))):
            kind = "incompatible-repository"
        return EngineError(kind, f"{operation} exited {result.returncode}", tail)

    # ---- engine surface ----------------------------------------------------

    def version(self) -> str:
        return self._run(["version"], expect_json=False).stdout.decode().strip()

    def init(self) -> None:
        self._run(["init"], expect_json=False)

    def snapshots(self, tag: str | None = None) -> list[Snapshot]:
        args = ["--json", "snapshots"]
        if tag is not None:
            args += ["--tag", tag]
        result = self._run(args)
        payload = json.loads(result.stdout.decode() or "[]")
        snapshots = [
            Snapshot(id=item["id"], time=item["time"],
                     paths=tuple(item.get("paths", ())),
                     tags=tuple(item.get("tags", ())))
            for item in payload
        ]
        # Ordering is ours, never restic's.
        return sorted(snapshots, key=lambda snapshot: snapshot.time)

    def backup(self, paths, tags) -> BackupResult:
        args = ["--json", "backup", "--no-scan",
                "--pack-size", str(self.config.pack_size_mb)]
        for tag in tags:
            args += ["--tag", tag]
        args += [str(path) for path in paths]
        # Exit 3 publishes a partial snapshot; the caller decides what that means.
        result = self._run(args, allow=(0, 3))
        snapshot_id = _summary_snapshot_id(result.stdout)
        if snapshot_id is None:
            raise EngineError("tool-error", "backup produced no summary event",
                              _tail(result.stderr))
        return BackupResult(snapshot_id, result.returncode)

    def ls(self, snapshot_id: str, path: str):
        # The path argument is required: without it restic also lists every
        # ancestor directory of the backed-up path.
        result = self._run(["--json", "ls", "--recursive", snapshot_id, path])
        for line in result.stdout.decode(errors="surrogateescape").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            if item.get("struct_type") != "node":
                continue
            yield Node(path=item["path"], type=item["type"], size=item.get("size"))

    @contextmanager
    def dump(self, snapshot_id: str, path: str, archive: str | None = None):
        args = self._base() + ["dump"]
        if archive:
            args += ["--archive", archive]
        args += [snapshot_id, path]
        try:
            process = subprocess.Popen(args, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE,
                                       env=self.config.restic_env())
        except OSError as error:
            raise EngineError("tool-error", f"cannot run {self.config.restic}: "
                                            f"{error}") from error
        # The adapter owns both pipes, including when a consumer or cleanup
        # raises. Reading to EOF and waiting do not close the Python handles.
        with process.stdout, process.stderr:
            consumer_error = None
            try:
                yield process.stdout
            except BaseException as error:
                consumer_error = error
                raise
            finally:
                # Consume to EOF, then wait: that is what terminates the process
                # cleanly. A failed process does not itself prove its stream was
                # corrupt. Documented operational failures override a consumer's
                # likely short-stream error, while an unknown exit must not erase
                # stronger integrity evidence already established by the consumer.
                try:
                    process.stdout.read()
                except (OSError, ValueError):
                    pass
                stderr = process.stderr.read()
                try:
                    returncode = process.wait(timeout=self.config.timeout_seconds)
                except subprocess.TimeoutExpired as error:
                    raise EngineError(
                        "tool-error",
                        f"restic dump timed out after {self.config.timeout_seconds}s") from error
                if returncode != 0:
                    failure = self._error(
                        subprocess.CompletedProcess(args, returncode, stderr=stderr),
                        operation="restic dump")
                    if consumer_error is None or failure.kind != "tool-error":
                        raise failure

    def check(self, read_data_subset: str | None = None) -> None:
        args = ["check"]
        if read_data_subset:
            args += ["--read-data-subset", read_data_subset]
        try:
            self._run(args, expect_json=False)
        except EngineError as error:
            # A completed check reporting damage is corruption evidence. Keep
            # documented connectivity/authentication/lock kinds distinct.
            if error.kind == "tool-error" and "exited" in error.message:
                raise EngineError("corrupt", error.message,
                                  error.stderr_tail) from error
            raise

    def unlock(self) -> str:
        result = self._run(["unlock"], expect_json=False)
        return result.stdout.decode(errors="replace").strip()

    def node_content_ids(self, snapshot_id: str, path: str) -> list[str]:
        """Blob ids of one file node, by walking the snapshot's tree blobs.

        `ls --json` nodes carry no `content`, so the walk goes through
        `cat snapshot` and `cat blob <tree>`, one tree per path component.
        """
        snapshot = json.loads(
            self._run(["--json", "cat", "snapshot", snapshot_id]).stdout)
        tree = snapshot["tree"]
        parts = [part for part in path.split("/") if part]
        node = None
        for index, name in enumerate(parts):
            nodes = json.loads(self._run(["cat", "blob", tree]).stdout)["nodes"]
            node = next((n for n in nodes if n.get("name") == name), None)
            if node is None:
                raise EngineError("missing", f"{path} is not in snapshot "
                                             f"{snapshot_id}")
            if index < len(parts) - 1:
                tree = node.get("subtree")
                if not tree:
                    raise EngineError("missing", f"{path}: {name} is not a "
                                                 f"directory in the snapshot")
        return list((node or {}).get("content") or [])

    # ---- helpers -----------------------------------------------------------

    @staticmethod
    def locate_export(paths, occ_id: str, attempt_id: str) -> str:
        """Find the catalog export by suffix, never by index."""
        suffix = f"/export/{occ_id}-{attempt_id}.sqlite"
        for path in paths:
            if path.endswith(suffix):
                return path
        raise EngineError("missing",
                          f"snapshot carries no export path ending {suffix}")


def _summary_snapshot_id(stdout: bytes) -> str | None:
    for line in stdout.decode(errors="surrogateescape").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("message_type") == "summary" and event.get("snapshot_id"):
            return event["snapshot_id"]
    return None


def _tail(stderr: bytes) -> str:
    text = stderr.decode(errors="replace").rstrip("\n")
    if not text:
        return ""
    return "\n".join(text.splitlines()[-STDERR_TAIL_LINES:])
