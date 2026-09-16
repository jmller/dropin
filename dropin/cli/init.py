"""`dropin init`: configuration, state directory, store, repository.

Runs before any configuration exists, so it takes its own arguments and writes
the file every other verb loads. It never destroys anything: an existing config
is refused without `--force`, an existing store is kept, and an existing
repository is attached rather than re-initialised.
"""

from __future__ import annotations

import os
from pathlib import Path
import secrets
import sys

from ..config import ConfigError, default_config_path, load
from ..report import EXIT_OK, EXIT_RUN_REFUSED, EXIT_USAGE

PASSWORD_OBLIGATION = (
    "The repository password file is the only key to the archive. Back it up "
    "somewhere that is not this machine; without it every archived item is "
    "unrecoverable. No additional encryption layer is needed or recommended.")

TEMPLATE = """[paths]
drop_dir  = {drop_dir}
state_dir = {state_dir}
[repository]
repo          = {repo}
password_file = {password_file}
[tools]
restic = "restic"
rclone = "rclone"
restic_min = "0.19.1"
rclone_min = "1.75.1"
rclone_connections = 2
timeout_seconds = 3600
pack_size_mb = 16
cache_max_mb = 2048
[drain]
settle_seconds = 5
sample_gap_seconds = 2
max_attempts = 3
retry_backoff_seconds = 300
[ownership]
lsof = "lsof"
[launchd]
label = {label}
interval = {interval}
"""


def run(args) -> int:
    config_path = _user_path(args.config) if args.config else default_config_path()
    missing = [flag for flag, value in (("--repo", args.repo),
                                        ("--drop-dir", args.drop_dir),
                                        ("--state-dir", args.state_dir))
               if not value]
    if missing:
        print(f"dropin init: required: {', '.join(missing)}", file=sys.stderr)
        return EXIT_USAGE
    if config_path.exists() and not args.force:
        print(f"dropin init: {config_path} exists; use --force to overwrite it",
              file=sys.stderr)
        return EXIT_USAGE

    drop_dir = _user_path(args.drop_dir)
    state_dir = _user_path(args.state_dir)
    password_file = _password_file(config_path, args)
    if args.password_file:
        if not password_file.exists():
            print(f"dropin init: password file {password_file} does not exist; "
                  f"create it with mode 0600 and back it up", file=sys.stderr)
            return EXIT_USAGE
    else:
        try:
            _create_password_file(password_file)
        except FileExistsError:
            pass  # Re-running init must retain the existing repository key.
        except OSError as error:
            print(f"dropin init: cannot create password file {password_file}: "
                  f"{error.strerror or error}", file=sys.stderr)
            return EXIT_USAGE
    for directory in (drop_dir, state_dir):
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    for name in ("export", "cache", "tmp"):
        (state_dir / name).mkdir(mode=0o700, exist_ok=True)

    label = args.label or "dev.dropin.drain"
    interval = args.interval or 900
    config_path.parent.mkdir(parents=True, exist_ok=True)
    text = TEMPLATE.format(drop_dir=_toml(drop_dir), state_dir=_toml(state_dir),
                           repo=_toml(args.repo), password_file=_toml(password_file),
                           label=_toml(label), interval=interval)
    # Validate before publishing the file, so a bad argument leaves no config.
    probe = config_path.with_name(config_path.name + ".tmp")
    probe.write_text(text)
    probe.chmod(0o600)
    try:
        config = load(probe)
    except ConfigError as error:
        probe.unlink()
        print(f"dropin init: {error}", file=sys.stderr)
        return EXIT_USAGE
    os.replace(probe, config_path)

    from . import Context
    from ..pipeline.writer_lock import LockHeld, writer_lock

    context = Context(config=config, json_output=args.json_output)
    try:
        with writer_lock(config.writer_lock_path, verb="init"):
            return _initialise(context, args, label, interval)
    except LockHeld as error:
        print(f"dropin init: {error}", file=sys.stderr)
        return EXIT_RUN_REFUSED


def _initialise(context, args, label: str, interval: int) -> int:
    from . import tools_gate
    from ..engine.interface import EngineError
    from ..store import records
    from ..store.db import connect

    config = context.config
    if config.store_path.exists():
        print(f"dropin init: keeping the existing store at {config.store_path}",
              file=sys.stderr)
    else:
        db = connect(config.store_path)
        try:
            records.initialise_store(db)
        finally:
            db.close()

    problem = tools_gate(context)
    if problem:
        print(f"dropin init: {problem}", file=sys.stderr)
        return EXIT_RUN_REFUSED
    try:
        context.engine.snapshots()
        print(f"dropin init: attached to the existing repository {config.repo}",
              file=sys.stderr)
    except EngineError as error:
        if error.kind != "no-repo":
            print(f"dropin init: repository {config.repo}: {error}",
                  file=sys.stderr)
            return EXIT_RUN_REFUSED
        try:
            context.engine.init()
        except EngineError as init_error:
            print(f"dropin init: cannot initialise {config.repo}: {init_error}",
                  file=sys.stderr)
            return EXIT_RUN_REFUSED
        print(f"dropin init: initialised repository {config.repo}",
              file=sys.stderr)

    print(PASSWORD_OBLIGATION, file=sys.stderr)

    if args.launchd:
        from .. import launchd

        agent = launchd.install(context.macos, config_path=config.path,
                                drop_dir=config.drop_dir, label=label,
                                interval=interval)
        print(launchd.bootstrap_command(agent.path))
    return EXIT_OK


def _password_file(config_path: Path, args) -> Path:
    """Return the explicit password path or the stable auto-generated path."""
    raw = args.password_file or config_path.with_name("repo.password")
    return _user_path(raw)


def _user_path(raw: str | os.PathLike[str]) -> Path:
    """Resolve a user-supplied path, including setup's literal ``~`` defaults."""
    return Path(os.path.realpath(os.path.expanduser(raw)))


def _create_password_file(path: Path) -> None:
    """Create a random password without following a race or overwriting one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as password:
            descriptor = None
            password.write(secrets.token_urlsafe(32).encode("ascii"))
            password.write(b"\n")
            password.flush()
            os.fsync(password.fileno())
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _toml(value) -> str:
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'
