"""The storage-engine seam: protocol, value types, and the tag grammar.

The repository is opaque, so identity lives entirely in seven reserved snapshot
tags. Parsing them is strict on purpose: a value that is not exactly canonical
is rejected rather than normalised, because normalising is how a traversal-
capable or ambiguous value becomes trusted.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterator, Protocol, runtime_checkable

STORE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
# Crockford's alphabet excludes I, L, O and U.
ULID_RE = re.compile(r"^[0-9ABCDEFGHJKMNPQRSTVWXYZ]{26}$")
IDENTIFIER_RE = re.compile(r"^[0-9a-f]{32}\.[0-9ABCDEFGHJKMNPQRSTVWXYZ]{26}$")
SEQ_RE = re.compile(r"^[1-9][0-9]*$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

TAG_PREFIX = "dropin:"
RESERVED_KEYS = ("v", "store", "occ", "attempt", "seq", "kind", "catalog-sha256")
KINDS = ("file", "dir", "bundle")
MAX_SEQ = 2 ** 63 - 2


class TagError(ValueError):
    """A snapshot's reserved tags are not exactly the canonical seven."""


class EngineError(Exception):
    """A storage-engine failure, classified so the pipeline can react.

    kind ∈ {no-repo, locked, bad-password, incompatible-repository, corrupt,
    missing, tool-error}.
    `partial` (restic exit 3) is not an error: it is a backup outcome.
    """

    def __init__(self, kind: str, message: str, stderr_tail: str = "") -> None:
        super().__init__(f"{kind}: {message}")
        self.kind = kind
        self.message = message
        self.stderr_tail = stderr_tail


@dataclass(frozen=True)
class Identity:
    """The seven reserved tags, parsed and validated."""

    store_id: str
    occ_id: str
    attempt_id: str
    export_seq: int
    kind: str
    catalog_sha256: str
    version: str = "1"

    def to_tags(self) -> list[str]:
        return [
            f"{TAG_PREFIX}v={self.version}",
            f"{TAG_PREFIX}store={self.store_id}",
            f"{TAG_PREFIX}occ={self.occ_id}",
            f"{TAG_PREFIX}attempt={self.attempt_id}",
            f"{TAG_PREFIX}seq={self.export_seq}",
            f"{TAG_PREFIX}kind={self.kind}",
            f"{TAG_PREFIX}catalog-sha256={self.catalog_sha256}",
        ]

    def tag_set(self) -> dict[str, str]:
        """The canonical comparison key: six identity values plus the version."""
        return {
            "v": self.version,
            "store": self.store_id,
            "occ": self.occ_id,
            "attempt": self.attempt_id,
            "seq": str(self.export_seq),
            "kind": self.kind,
            "catalog-sha256": self.catalog_sha256,
        }


@dataclass(frozen=True)
class Snapshot:
    id: str
    time: str
    paths: tuple[str, ...]
    tags: tuple[str, ...]


@dataclass(frozen=True)
class Node:
    """One `ls --json` entry. 0.19.1 exposes no link target here."""

    path: str
    type: str
    size: int | None = None


@dataclass(frozen=True)
class BackupResult:
    snapshot_id: str
    exit_code: int


def parse_tags(tags) -> Identity:
    """Strictly parse the seven reserved tags. Anything off-grammar raises."""
    seen: dict[str, str] = {}
    for tag in tags:
        if not tag.startswith(TAG_PREFIX):
            continue  # non-reserved tags are allowed but are not evidence
        body = tag[len(TAG_PREFIX):]
        key, separator, value = body.partition("=")
        if key not in RESERVED_KEYS:
            continue
        if not separator:
            raise TagError(f"reserved tag {key!r} carries no value")
        if key in seen:
            raise TagError(f"duplicate reserved tag {key!r}")
        seen[key] = value

    missing = [key for key in RESERVED_KEYS if key not in seen]
    if missing:
        raise TagError(f"missing reserved tags: {', '.join(missing)}")

    if seen["v"] != "1":
        raise TagError(f"unsupported tag version {seen['v']!r}")
    if not STORE_ID_RE.match(seen["store"]):
        raise TagError(f"store must be 32 lowercase hex, got {seen['store']!r}")
    if seen["kind"] not in KINDS:
        raise TagError(f"kind must be one of {KINDS}, got {seen['kind']!r}")
    if not SHA256_RE.match(seen["catalog-sha256"]):
        raise TagError("catalog-sha256 must be 64 lowercase hex, got "
                       f"{seen['catalog-sha256']!r}")
    if not SEQ_RE.match(seen["seq"]):
        raise TagError(f"seq must be canonical unsigned decimal, got {seen['seq']!r}")
    export_seq = int(seen["seq"])
    if export_seq > MAX_SEQ:
        raise TagError(f"seq out of range: {seen['seq']!r}")
    for key in ("occ", "attempt"):
        value = seen[key]
        if not IDENTIFIER_RE.match(value):
            raise TagError(f"{key} is not a namespaced identifier: {value!r}")
        if not value.startswith(f"{seen['store']}."):
            raise TagError(f"{key} namespace does not match store: {value!r}")

    return Identity(store_id=seen["store"], occ_id=seen["occ"],
                    attempt_id=seen["attempt"], export_seq=export_seq,
                    kind=seen["kind"], catalog_sha256=seen["catalog-sha256"],
                    version=seen["v"])


@runtime_checkable
class Engine(Protocol):
    """Everything the pipeline is allowed to ask of the repository."""

    def version(self) -> str: ...

    def init(self) -> None: ...

    def snapshots(self, tag: str | None = None) -> list[Snapshot]: ...

    def backup(self, paths, tags) -> BackupResult: ...

    def ls(self, snapshot_id: str, path: str) -> Iterator[Node]: ...

    def dump(self, snapshot_id: str, path: str, archive: str | None = None): ...

    def check(self, read_data_subset: str | None = None) -> None: ...

    def unlock(self) -> str: ...

    def node_content_ids(self, snapshot_id: str, path: str) -> list[str]: ...
