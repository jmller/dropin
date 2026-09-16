"""Configuration: explicit over implicit.

One TOML file, two explicit overrides (`--config`, `$DROPIN_CONFIG`), no derived
magic paths. Unknown keys are an error so a typo can never silently disable a
setting, and the restic environment is built rather than inherited so a stray
shell variable cannot redirect the repository.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import stat
import tomllib

DEFAULT_CONFIG_PATH = Path("~/.config/dropin/config.toml")
CONFIG_ENV = "DROPIN_CONFIG"

# Every accepted key, by table. Anything else is a typo (exit 2).
SCHEMA: dict[str, dict[str, type | tuple[type, ...]]] = {
    "paths": {"drop_dir": str, "state_dir": str},
    "repository": {"repo": str, "password_file": str},
    "tools": {"restic": str, "rclone": str, "restic_min": str, "rclone_min": str,
              "rclone_connections": int, "timeout_seconds": int,
              "pack_size_mb": int, "cache_max_mb": int},
    "drain": {"settle_seconds": (int, float), "sample_gap_seconds": (int, float),
              "max_attempts": int, "retry_backoff_seconds": (int, float)},
    "ownership": {"lsof": str},
    "launchd": {"label": str, "interval": int},
    # Optional and default-disabled. The element/path checks are stricter than
    # the outer TOML list type and run after drop/state resolution.
    "restore": {"destinations": list},
}
OPTIONAL_TABLES = {"restore"}

POSITIVE_NUMERIC = {
    ("tools", "rclone_connections"), ("tools", "timeout_seconds"),
    ("tools", "pack_size_mb"), ("tools", "cache_max_mb"),
    ("drain", "max_attempts"), ("drain", "retry_backoff_seconds"),
    ("launchd", "interval"),
}

# Zero is meaningful here: it disables the wait, not the check.
NON_NEGATIVE_NUMERIC = {
    ("drain", "settle_seconds"), ("drain", "sample_gap_seconds"),
}

# Passed through to restic when the caller has it; everything else RESTIC_*/
# RCLONE_* is stripped.
PASSTHROUGH = ("RCLONE_CONFIG",)


class ConfigError(Exception):
    """Invalid configuration. Always names the offending key or path."""


@dataclass(frozen=True)
class Config:
    path: Path
    drop_dir: Path
    state_dir: Path
    repo: str
    password_file: Path
    restic: str
    rclone: str
    restic_min: str
    rclone_min: str
    rclone_connections: int
    timeout_seconds: int
    pack_size_mb: int
    cache_max_mb: int
    settle_seconds: float
    sample_gap_seconds: float
    max_attempts: int
    retry_backoff_seconds: float
    lsof: str
    launchd_label: str
    launchd_interval: int
    restore_destinations: tuple[Path, ...]

    # Derived state-directory layout. Nothing outside it is ever written.
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

    @property
    def writer_lock_path(self) -> Path:
        return self.state_dir / "writer.lock"

    @property
    def recover_tmp_dir(self) -> Path:
        return self.state_dir / "recover.tmp"

    @property
    def restore_dir(self) -> Path:
        return self.state_dir / "restore"

    @property
    def restore_state_path(self) -> Path:
        return self.restore_dir / "requests.sqlite"

    def restic_env(self, base: dict[str, str] | None = None) -> dict[str, str]:
        """The exact environment every restic subprocess gets."""
        source = dict(os.environ if base is None else base)
        env = {key: value for key, value in source.items()
               if not key.startswith(("RESTIC_", "RCLONE_"))}
        for key in PASSTHROUGH:
            if key in source:
                env[key] = source[key]
        env["RESTIC_PASSWORD_FILE"] = str(self.password_file)
        env["RESTIC_CACHE_DIR"] = str(self.cache_dir)
        env["TMPDIR"] = str(self.tmp_dir)
        return env


def default_config_path() -> Path:
    override = os.environ.get(CONFIG_ENV)
    if override:
        return Path(override)
    return DEFAULT_CONFIG_PATH.expanduser()


def load(path: Path | str | None = None) -> Config:
    path = Path(path) if path is not None else default_config_path()
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise ConfigError(f"config file {path}: {error.strerror or error}") from error
    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        raise ConfigError(f"config file {path}: {error}") from error

    _reject_unknown(document)
    values = _typed_values(document)
    drop_dir, state_dir = _resolve_directories(values)
    restore_destinations = _restore_directories(
        values["restore"]["destinations"], drop_dir, state_dir)
    password_file = _check_password_file(values["repository"]["password_file"])
    _check_repo(values["repository"]["repo"])

    return Config(
        path=path,
        drop_dir=drop_dir,
        state_dir=state_dir,
        repo=values["repository"]["repo"],
        password_file=password_file,
        restic=values["tools"]["restic"],
        rclone=values["tools"]["rclone"],
        restic_min=values["tools"]["restic_min"],
        rclone_min=values["tools"]["rclone_min"],
        rclone_connections=values["tools"]["rclone_connections"],
        timeout_seconds=values["tools"]["timeout_seconds"],
        pack_size_mb=values["tools"]["pack_size_mb"],
        cache_max_mb=values["tools"]["cache_max_mb"],
        settle_seconds=values["drain"]["settle_seconds"],
        sample_gap_seconds=values["drain"]["sample_gap_seconds"],
        max_attempts=values["drain"]["max_attempts"],
        retry_backoff_seconds=values["drain"]["retry_backoff_seconds"],
        lsof=values["ownership"]["lsof"],
        launchd_label=values["launchd"]["label"],
        launchd_interval=values["launchd"]["interval"],
        restore_destinations=restore_destinations,
    )


def _reject_unknown(document: dict) -> None:
    for table, entries in document.items():
        if table not in SCHEMA:
            raise ConfigError(f"unknown configuration table [{table}]")
        if not isinstance(entries, dict):
            raise ConfigError(f"[{table}] must be a table")
        for key in entries:
            if key not in SCHEMA[table]:
                raise ConfigError(f"unknown configuration key {table}.{key}")


def _typed_values(document: dict) -> dict[str, dict]:
    values: dict[str, dict] = {}
    for table, keys in SCHEMA.items():
        entries = document.get(table)
        if entries is None:
            if table in OPTIONAL_TABLES:
                entries = {"destinations": []}
            else:
                raise ConfigError(f"missing configuration table [{table}]")
        values[table] = {}
        for key, expected in keys.items():
            if key not in entries:
                raise ConfigError(f"missing configuration key {table}.{key}")
            value = entries[key]
            # bool is an int subclass; a boolean here is always a mistake.
            if isinstance(value, bool) or not isinstance(value, expected):
                raise ConfigError(
                    f"{table}.{key} must be {_name(expected)}, got {value!r}")
            if (table, key) in POSITIVE_NUMERIC and value <= 0:
                raise ConfigError(f"{table}.{key} must be positive, got {value!r}")
            if (table, key) in NON_NEGATIVE_NUMERIC and value < 0:
                raise ConfigError(
                    f"{table}.{key} must not be negative, got {value!r}")
            values[table][key] = value
    return values


def _name(expected: type | tuple[type, ...]) -> str:
    if isinstance(expected, tuple):
        return " or ".join(item.__name__ for item in expected)
    return expected.__name__


def _resolve_directories(values: dict[str, dict]) -> tuple[Path, Path]:
    drop_dir = Path(os.path.realpath(values["paths"]["drop_dir"]))
    state_dir = Path(os.path.realpath(values["paths"]["state_dir"]))
    if not drop_dir.is_dir():
        raise ConfigError(f"paths.drop_dir does not exist: {drop_dir}")
    if not state_dir.is_dir():
        raise ConfigError(f"paths.state_dir does not exist: {state_dir}")
    # Overlap in either direction would let the archiver archive its own state
    # or evict it. Symlink aliases are resolved above, so this catches
    # them too.
    if drop_dir == state_dir:
        raise ConfigError("paths.drop_dir and paths.state_dir must differ")
    if _contains(state_dir, drop_dir):
        raise ConfigError("paths.drop_dir must not be inside paths.state_dir")
    if _contains(drop_dir, state_dir):
        raise ConfigError("paths.state_dir must not be inside paths.drop_dir")
    return drop_dir, state_dir


def _contains(parent: Path, child: Path) -> bool:
    return parent in child.parents


def _restore_directories(raw_paths: list, drop_dir: Path,
                         state_dir: Path) -> tuple[Path, ...]:
    destinations: list[Path] = []
    for index, raw in enumerate(raw_paths):
        key = f"restore.destinations[{index}]"
        if not isinstance(raw, str) or not raw:
            raise ConfigError(f"{key} must be a non-empty string")
        if not os.path.isabs(raw):
            raise ConfigError(f"{key} must be an absolute path")
        if os.path.normpath(raw) != raw:
            raise ConfigError(f"{key} must use exact canonical spelling without . or ..")
        path = Path(raw)
        # Do not touch the enrolled path during global config loading. A future
        # terminal replay must remain possible after its output directory was
        # moved or removed. Explicit profile initialization/nonterminal work
        # performs the live no-symlink, ownership, mode, identity and alias gate.
        for forbidden, name in ((drop_dir, "paths.drop_dir"),
                                (state_dir, "paths.state_dir")):
            if (path == forbidden or _contains(path, forbidden)
                    or _contains(forbidden, path)):
                raise ConfigError(f"{key} must not overlap {name}")
        if path in destinations:
            raise ConfigError(f"{key} duplicates an enrolled spelling")
        for other in destinations:
            if path == other or _contains(path, other) or _contains(other, path):
                raise ConfigError(f"{key} overlaps another restore destination")
        destinations.append(path)
    return tuple(destinations)


def _check_password_file(raw: str) -> Path:
    path = Path(os.path.realpath(raw))
    try:
        mode = path.stat().st_mode
    except OSError as error:
        raise ConfigError(
            f"repository.password_file {path}: {error.strerror or error}") from error
    if mode & (stat.S_IRGRP | stat.S_IROTH | stat.S_IWGRP | stat.S_IWOTH):
        raise ConfigError(
            f"repository.password_file {path} is group/world accessible "
            f"(mode {stat.filemode(mode)}); use 0600")
    return path


def _check_repo(repo: str) -> None:
    if not repo.startswith("rclone:"):
        raise ConfigError("repository.repo must start with 'rclone:'")
    rest = repo[len("rclone:"):]
    remote, separator, path = rest.partition(":")
    if not separator or not remote:
        raise ConfigError(
            "repository.repo must be rclone:<remote>:<path> with a non-empty remote")
    if not path.startswith("/"):
        raise ConfigError(
            "repository.repo path must be absolute (for example, "
            "rclone:<remote>:/archive) so it does not depend on the working directory")
