"""In-memory repository fake.

Deliberately unhelpful in the ways the real engine is unhelpful: snapshots come
back unsorted, `ls` nodes carry no link target, and nothing reports a capability
it has not been configured to have. Corruption, truncation, missing exports and
partial backups are injectable, because those are the paths that must never
silently succeed.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import io
import posixpath
import tarfile
import tempfile
import time

from .interface import BackupResult, EngineError, Node, Snapshot


@dataclass
class _Source:
    type: str
    content: bytes = b""
    link_target: str | None = None


@dataclass
class _Snapshot:
    id: str
    time: str
    paths: tuple[str, ...]
    tags: tuple[str, ...]
    entries: dict[str, _Source]
    corrupted: set[str] = field(default_factory=set)
    flipped: set[str] = field(default_factory=set)
    dropped: set[str] = field(default_factory=set)
    truncated: bool = False
    renamed: dict[str, str] = field(default_factory=dict)
    duplicated: set[str] = field(default_factory=set)


class FakeEngine:
    def __init__(self) -> None:
        self.sources: dict[str, _Source] = {}
        self._snapshots: list[_Snapshot] = []
        self.calls: list[tuple] = []
        self._failure: str | None = None
        self._raise_on_any_call = False
        self._exit3: tuple[bool, str | None] = (False, None)
        self._counter = 0
        self.initialised = False

    # ---- source fixture setup ---------------------------------------------

    def add_source_file(self, path: str, content: bytes) -> None:
        self.sources[path] = _Source("file", content)
        self._add_parents(path)

    def add_source_dir(self, path: str) -> None:
        self.sources[path] = _Source("dir")
        self._add_parents(path)

    def add_source_symlink(self, path: str, target: str) -> None:
        self.sources[path] = _Source("symlink", link_target=target)
        self._add_parents(path)

    def _add_parents(self, path: str) -> None:
        parent = posixpath.dirname(path)
        while parent and parent != "/":
            self.sources.setdefault(parent, _Source("dir"))
            parent = posixpath.dirname(parent)

    # ---- injection ---------------------------------------------------------

    def fail_with(self, kind: str | None) -> None:
        self._failure = kind

    def raise_on_any_call(self) -> None:
        self._raise_on_any_call = True

    def exit3_on_next_backup(self, omit: str | None = None) -> None:
        self._exit3 = (True, omit)

    def inject_corruption(self, snapshot_id: str, path: str,
                          flip_byte: bool = False) -> None:
        snapshot = self._find(snapshot_id)
        (snapshot.flipped if flip_byte else snapshot.corrupted).add(path)

    def inject_truncation(self, snapshot_id: str) -> None:
        self._find(snapshot_id).truncated = True

    def drop_export(self, snapshot_id: str) -> None:
        snapshot = self._find(snapshot_id)
        for path in list(snapshot.entries):
            if path.endswith(".sqlite"):
                snapshot.dropped.add(path)

    # ---- engine surface ----------------------------------------------------

    def _guard(self, name: str, *details) -> None:
        if self._raise_on_any_call:
            raise AssertionError(f"engine must not be constructed or called: {name}")
        self.calls.append((name, *details))
        if self._failure:
            raise EngineError(self._failure, f"injected failure on {name}")

    def version(self) -> str:
        self._guard("version")
        return "restic 0.19.1 (fake)"

    def init(self) -> None:
        self._guard("init")
        self.initialised = True

    def backup(self, paths, tags) -> BackupResult:
        self._guard("backup", tuple(paths), tuple(tags))
        partial, omit = self._exit3
        self._exit3 = (False, None)
        entries: dict[str, _Source] = {}
        for root in paths:
            if root not in self.sources:
                # The real engine reads the disk. A root nobody configured —
                # the pipeline's own catalog export, typically — is read the
                # same way, so the fake cannot pass by skipping it.
                self._read_from_disk(root)
            for path, source in self.sources.items():
                if path == root or path.startswith(root.rstrip("/") + "/"):
                    if partial and omit and path == omit:
                        continue
                    entries[path] = source
        self._counter += 1
        snapshot = _Snapshot(
            id=hashlib.sha256(f"snapshot-{self._counter}".encode()).hexdigest(),
            time=self._next_time(), paths=tuple(paths), tags=tuple(tags),
            entries=entries)
        self._snapshots.append(snapshot)
        return BackupResult(snapshot.id, 3 if partial else 0)

    def _read_from_disk(self, root: str) -> None:
        import os

        if os.path.islink(root):
            self.add_source_symlink(root, os.readlink(root))
        elif os.path.isdir(root):
            self.add_source_dir(root)
            for dirpath, dirnames, filenames in os.walk(root):
                for name in dirnames:
                    self._read_from_disk(os.path.join(dirpath, name))
                for name in filenames:
                    self._read_from_disk(os.path.join(dirpath, name))
                dirnames[:] = []  # recursion above already descended
        elif os.path.isfile(root):
            with open(root, "rb") as handle:
                self.add_source_file(root, handle.read())

    def _next_time(self) -> str:
        stamp = time.gmtime(1_780_000_000 + self._counter * 61)
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", stamp)

    def snapshots(self, tag: str | None = None) -> list[Snapshot]:
        self._guard("snapshots", tag)
        selected = [s for s in self._snapshots if tag is None or tag in s.tags]
        # Hand them back newest-first so a caller that forgets to sort by `time`
        # fails its own test rather than in production.
        return [Snapshot(s.id, s.time, s.paths, s.tags) for s in reversed(selected)]

    def snapshot(self, snapshot_id: str) -> Snapshot:
        found = self._find(snapshot_id)
        return Snapshot(found.id, found.time, found.paths, found.tags)

    def ls(self, snapshot_id: str, path: str):
        self._guard("ls", snapshot_id, path)
        snapshot = self._find(snapshot_id)
        prefix = path.rstrip("/")
        for entry_path in sorted(snapshot.entries):
            if entry_path != prefix and not entry_path.startswith(prefix + "/"):
                continue
            source = snapshot.entries[entry_path]
            yield Node(path=entry_path, type=source.type,
                       size=len(source.content) if source.type == "file" else None)

    @contextmanager
    def dump(self, snapshot_id: str, path: str, archive: str | None = None):
        self._guard("dump", snapshot_id, path, archive)
        snapshot = self._find(snapshot_id)
        if path in snapshot.dropped or path not in snapshot.entries:
            raise EngineError("missing", f"{path} is not in snapshot {snapshot_id}")
        if path in snapshot.corrupted:
            raise EngineError("corrupt", f"ciphertext verification failed: {path}")
        if archive == "tar":
            stream = self._tar_bytes(snapshot, path)
            try:
                yield stream
            finally:
                stream.close()
        else:
            source = snapshot.entries[path]
            content = source.content
            if path in snapshot.flipped:
                content = bytes([content[0] ^ 0xFF]) + content[1:]
            yield io.BytesIO(content)

    def _tar_bytes(self, snapshot: _Snapshot, root: str):
        # Written to a temp file rather than an in-memory buffer: real restic
        # streams, and a fake that materialises the whole archive would make
        # every memory assertion measure the fake instead of the code.
        handle = tempfile.NamedTemporaryFile(prefix="dropin-fake-tar-")
        with tarfile.open(fileobj=handle, mode="w") as tar:
            for entry_path in sorted(snapshot.entries):
                if entry_path == root:
                    # restic emits the descendants only, never the dumped
                    # directory itself.
                    continue
                if not entry_path.startswith(root.rstrip("/") + "/"):
                    continue
                source = snapshot.entries[entry_path]
                member_name = snapshot.renamed.get(entry_path,
                                                   entry_path.lstrip("/"))
                info = tarfile.TarInfo(member_name)
                if source.type == "dir":
                    info.type = tarfile.DIRTYPE
                    tar.addfile(info)
                elif source.type == "symlink":
                    info.type = tarfile.SYMTYPE
                    info.linkname = source.link_target or ""
                    tar.addfile(info)
                else:
                    content = source.content
                    if entry_path in snapshot.flipped:
                        content = bytes([content[0] ^ 0xFF]) + content[1:]
                    info.size = len(content)
                    tar.addfile(info, io.BytesIO(content))
                    if entry_path in snapshot.duplicated:
                        repeat = tarfile.TarInfo(member_name)
                        repeat.size = len(content)
                        tar.addfile(repeat, io.BytesIO(content))
        handle.flush()
        handle.seek(0)
        if not snapshot.truncated:
            return handle
        # A killed `dump` stops where it stopped: no end-of-archive marker.
        # That is what the reader detects. Note what it does *not*
        # detect: a cut landing on a member boundary parses cleanly with fewer
        # members, which is why payload verification compares the member set
        # against the manifest instead of trusting a successful parse.
        data = handle.read().rstrip(b"\x00")
        handle.close()
        return io.BytesIO(data)

    def check(self, read_data_subset: str | None = None) -> None:
        self._guard("check", read_data_subset)

    def unlock(self) -> str:
        self._guard("unlock")
        return "successfully checked and removed stale repository locks (fake)"

    def node_content_ids(self, snapshot_id: str, path: str) -> list[str]:
        self._guard("node_content_ids", snapshot_id, path)
        snapshot = self._find(snapshot_id)
        source = snapshot.entries[path]
        # Content-addressed, exactly like the real thing: identical bytes share
        # blob identity across snapshots (the dedup oracle).
        return [hashlib.sha256(source.content).hexdigest()]

    def _find(self, snapshot_id: str) -> _Snapshot:
        for snapshot in self._snapshots:
            if snapshot.id == snapshot_id:
                return snapshot
        raise EngineError("missing", f"no such snapshot {snapshot_id}")

    # ---- adversarial stream injection -------------------------------

    def rewrite_tar_member(self, snapshot_id: str, path: str, name: str) -> None:
        """Emit one member under an attacker-chosen name."""
        self._find(snapshot_id).renamed[path] = name

    def duplicate_tar_member(self, snapshot_id: str, path: str) -> None:
        self._find(snapshot_id).duplicated.add(path)
