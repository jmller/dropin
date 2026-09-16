"""Versioned local restore-history foundation for reserved macOS retrieval.

This module validates exact private destinations and creates/inspects the separate
request journal. Schema version 1 remains activation-blocked for the deferred
automatic-convergence protocol. Normal Darwin retrieval does not read or mutate
this journal; ``validate_reserved_destination`` serves only the optional profile.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from importlib.resources import files
import json
import os
from pathlib import Path
import platform
import re
import secrets
import sqlite3
import stat
from urllib.parse import quote

from ..config import Config

SCHEMA_VERSION = 1
PROTOCOL_VERSION = 1
BLOCKED_GATES = ("strict-journal", "apfs-persistence", "deployment-acceptance")
HEX32 = re.compile(r"[0-9a-f]{32}\Z")
EXPECTED_TABLES = {
    "restore_meta", "restore_destination", "restore_request", "restore_item",
    "restore_alias", "restore_attempt", "restore_transition",
}


class RestoreStateError(Exception):
    """A fail-closed profile/configuration refusal."""

    def __init__(self, reason: str, *, kind: str = "state") -> None:
        super().__init__(reason)
        self.kind = kind


@dataclass(frozen=True)
class DestinationProfile:
    destination_id: str
    path: str
    binding: str


@dataclass(frozen=True)
class Profile:
    generation: str
    schema_version: int
    protocol_version: int
    activation: str
    blocked_gates: tuple[str, ...]
    destinations: tuple[DestinationProfile, ...]

    def to_dict(self) -> dict:
        return {
            "generation": self.generation,
            "schema_version": self.schema_version,
            "protocol_version": self.protocol_version,
            "activation": self.activation,
            "blocked_gates": list(self.blocked_gates),
            "destinations": [
                {"destination_id": item.destination_id, "path": item.path}
                for item in self.destinations
            ],
        }


def validate_reserved_destination(config: Config, destination: os.PathLike | str) -> Path:
    """Validate one exact live destination for the optional history profile."""
    raw = os.fspath(destination)
    path = Path(raw)
    if not path.is_absolute() or raw != str(path) or path not in config.restore_destinations:
        raise RestoreStateError(
            f"restore profile requires an exact [restore] enrolled destination: {raw}",
            kind="usage")
    binding = json.loads(_binding(path))
    destination_ids = {
        (item["device"], item["inode"]) for item in binding["components"]
    }
    destination_id = (
        binding["components"][-1]["device"],
        binding["components"][-1]["inode"],
    )
    for protected, name in ((config.drop_dir, "paths.drop_dir"),
                            (config.state_dir, "paths.state_dir")):
        protected_ids = _path_identities(protected)
        if protected_ids[-1] in destination_ids or destination_id in protected_ids:
            raise RestoreStateError(
                f"restore profile destination overlaps {name} by filesystem identity: {path}",
                kind="usage")
    return path


def initialize(config: Config) -> Profile:
    """Explicitly create a new activation-blocked restore profile.

    Existing or interrupted history is never overwritten.  This writes only
    below the configured state directory and merely observes enrolled outputs.
    """
    if not config.restore_destinations:
        raise RestoreStateError("no restore destinations are enrolled in [restore]",
                                kind="usage")
    bindings = _validated_enrollment(config)
    restore_dir = _restore_directory(config)
    interrupted = _interrupted(config)
    if config.restore_state_path.exists():
        raise RestoreStateError(
            f"restore history already exists: {config.restore_state_path}",
            kind="usage")
    if interrupted:
        raise RestoreStateError(
            f"interrupted initialization requires inspection: {interrupted[0]}")

    generation = secrets.token_hex(16)
    suffix = secrets.token_hex(8)
    temporary = restore_dir / f".requests.sqlite.init-{suffix}"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600)
    os.close(descriptor)
    published = False
    try:
        db = sqlite3.connect(temporary, isolation_level=None)
        try:
            db.execute("PRAGMA foreign_keys=ON")
            if db.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
                raise RestoreStateError("cannot select DELETE restore journal mode")
            db.execute("PRAGMA synchronous=EXTRA")
            schema = files("dropin.restore_state.schema").joinpath(
                "0001_initial.sql").read_text(encoding="utf-8")
            db.executescript("BEGIN;\n" + schema + "\nCOMMIT;")
            now = _now()
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute(
                    "INSERT INTO restore_meta VALUES (1,?,?,?,?,?,?)",
                    (SCHEMA_VERSION, PROTOCOL_VERSION, generation, now, "blocked",
                     _json(BLOCKED_GATES)))
                for path in config.restore_destinations:
                    db.execute(
                        "INSERT INTO restore_destination(destination_id,exact_path,binding_json,created_at) "
                        "VALUES (?,?,?,?)",
                        (secrets.token_hex(16), str(path), bindings[path], now))
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RestoreStateError("new restore history failed integrity check")
        finally:
            db.close()
        os.chmod(temporary, 0o600)
        _fsync_file(temporary)
        # link is an atomic no-overwrite publication on the same filesystem.
        os.link(temporary, config.restore_state_path, follow_symlinks=False)
        published = True
        temporary.unlink()
        _fsync_directory(restore_dir)
        return inspect(config)
    except FileExistsError as error:
        raise RestoreStateError(
            f"restore history already exists: {config.restore_state_path}",
            kind="usage") from error
    except (OSError, sqlite3.Error) as error:
        raise RestoreStateError(f"restore history initialization failed: {error}") from error
    finally:
        if not published:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def inspect(config: Config) -> Profile:
    """Open and validate existing state without creating/upgrading anything."""
    try:
        directory_info = config.restore_dir.lstat()
    except FileNotFoundError:
        raise RestoreStateError(
            f"restore state is not initialized: {config.restore_state_path}") from None
    _validate_private_directory(config.restore_dir, directory_info)
    interrupted = _interrupted(config)
    if interrupted:
        raise RestoreStateError(
            f"interrupted initialization requires inspection: {interrupted[0]}")
    try:
        history_info = config.restore_state_path.lstat()
    except FileNotFoundError:
        raise RestoreStateError(
            f"restore state is not initialized: {config.restore_state_path}") from None
    if (not stat.S_ISREG(history_info.st_mode) or stat.S_ISLNK(history_info.st_mode)
            or history_info.st_uid != os.geteuid()
            or stat.S_IMODE(history_info.st_mode) != 0o600):
        raise RestoreStateError(
            f"restore history must be a real owned mode-0600 file: {config.restore_state_path}")
    try:
        uri = "file:" + quote(str(config.restore_state_path), safe="/") + "?mode=ro"
        db = sqlite3.connect(uri, uri=True)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA query_only=ON")
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RestoreStateError("restore history integrity check failed")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version != SCHEMA_VERSION:
                raise RestoreStateError(
                    f"restore history schema version {version} is incompatible; expected {SCHEMA_VERSION}")
            tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if tables != EXPECTED_TABLES:
                raise RestoreStateError("restore history schema differs")
            meta = db.execute("SELECT * FROM restore_meta").fetchall()
            if len(meta) != 1:
                raise RestoreStateError("restore history metadata differs")
            row = meta[0]
            gates = tuple(json.loads(row["blocked_gates_json"]))
            if (row["schema_version"] != SCHEMA_VERSION
                    or row["protocol_version"] != PROTOCOL_VERSION
                    or not HEX32.fullmatch(row["generation"])
                    or row["activation"] != "blocked"
                    or gates != BLOCKED_GATES):
                raise RestoreStateError("restore history profile differs")
            persisted = {item["exact_path"]: item for item in db.execute(
                "SELECT * FROM restore_destination")}
            expected = tuple(str(path) for path in config.restore_destinations)
            if set(persisted) != set(expected):
                raise RestoreStateError("restore destination enrollment differs from config")
            destinations = []
            for path in config.restore_destinations:
                item = persisted[str(path)]
                binding = _binding(path)
                if item["binding_json"] != binding:
                    raise RestoreStateError(
                        f"restore destination binding differs: {path}")
                if not HEX32.fullmatch(item["destination_id"]):
                    raise RestoreStateError("restore destination identity differs")
                destinations.append(DestinationProfile(
                    item["destination_id"], item["exact_path"], binding))
            if db.execute("PRAGMA foreign_key_check").fetchall():
                raise RestoreStateError("restore history foreign keys differ")
            return Profile(row["generation"], row["schema_version"],
                           row["protocol_version"], row["activation"], gates,
                           tuple(destinations))
        finally:
            db.close()
    except RestoreStateError:
        raise
    except (OSError, sqlite3.Error, UnicodeError, ValueError, TypeError,
            KeyError, json.JSONDecodeError) as error:
        raise RestoreStateError(f"restore history is unavailable or corrupt: {error}") from error


def parse_request_id(profile: Profile, value: str) -> str:
    """Validate caller-owned GENERATION:TOKEN against the open profile."""
    if not isinstance(value, str):
        raise RestoreStateError("request id must be GENERATION:TOKEN", kind="usage")
    generation, separator, token = value.partition(":")
    if (not separator or not HEX32.fullmatch(generation)
            or not HEX32.fullmatch(token)):
        raise RestoreStateError("request id must be lowercase-hex GENERATION:TOKEN",
                                kind="usage")
    if generation != profile.generation:
        raise RestoreStateError("request generation differs from restore history",
                                kind="usage")
    return token


def _restore_directory(config: Config) -> Path:
    try:
        config.restore_dir.mkdir(mode=0o700)
    except FileExistsError:
        pass
    try:
        info = config.restore_dir.lstat()
    except OSError as error:
        raise RestoreStateError(f"restore state directory unavailable: {error}") from error
    _validate_private_directory(config.restore_dir, info)
    return config.restore_dir


def _validate_private_directory(path: Path, info: os.stat_result) -> None:
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700):
        raise RestoreStateError(
            f"restore state directory must be an owned real mode-0700 directory: {path}")


def _interrupted(config: Config) -> list[Path]:
    try:
        return sorted(config.restore_dir.glob(".requests.sqlite.init-*"))
    except OSError as error:
        raise RestoreStateError(f"restore state directory unavailable: {error}") from error


def _validated_enrollment(config: Config) -> dict[Path, str]:
    bindings: dict[Path, str] = {}
    identities: set[tuple[int, int]] = set()
    resolved: list[Path] = []
    for path in config.restore_destinations:
        validate_reserved_destination(config, path)
        binding = _binding(path)
        document = json.loads(binding)
        final = document["components"][-1]
        identity = final["device"], final["inode"]
        if identity in identities:
            raise RestoreStateError(f"restore destination filesystem alias duplicated: {path}")
        actual = Path(os.path.realpath(path))
        for other in resolved:
            if actual == other or other in actual.parents or actual in other.parents:
                raise RestoreStateError(f"restore destinations overlap by filesystem identity: {path}")
        identities.add(identity)
        resolved.append(actual)
        bindings[path] = binding
    return bindings


def _binding(path: Path) -> str:
    try:
        components = []
        current = Path(path.anchor)
        root = current.lstat()
        components.append(_binding_record(current, root))
        for part in path.parts[1:]:
            current /= part
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise RestoreStateError(f"restore destination binding differs: {path}")
            components.append(_binding_record(current, info))
    except RestoreStateError:
        raise
    except OSError as error:
        raise RestoreStateError(
            f"restore destination binding differs: {path}: {error}") from error
    final = components[-1]
    if final["uid"] != os.geteuid() or final["mode"] != 0o700:
        raise RestoreStateError(f"restore destination binding differs: {path}")
    return _json({
        "version": 1,
        "platform": platform.system(),
        "machine": platform.machine(),
        "exact_path": str(path),
        "components": components,
    })


def _path_identities(path: Path) -> tuple[tuple[int, int], ...]:
    """Return root-to-leaf directory identities, following configured spelling."""
    try:
        identities = []
        current = Path(path.anchor)
        for part in (None, *path.parts[1:]):
            if part is not None:
                current /= part
            info = current.stat()
            if not stat.S_ISDIR(info.st_mode):
                raise RestoreStateError(f"configured directory binding differs: {path}")
            identities.append((info.st_dev, info.st_ino))
        return tuple(identities)
    except RestoreStateError:
        raise
    except OSError as error:
        raise RestoreStateError(
            f"configured directory binding differs: {path}: {error}") from error


def _binding_record(path: Path, info: os.stat_result) -> dict:
    return {
        "name": str(path), "device": info.st_dev, "inode": info.st_ino,
        "type": stat.S_IFMT(info.st_mode), "uid": info.st_uid,
        "mode": stat.S_IMODE(info.st_mode),
    }


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=True,
                      separators=(",", ":"))


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
