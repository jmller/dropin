"""Remove Dropin's local installation, with an explicit destructive purge."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib

from ..config import default_config_path
from ..report import EXIT_OK, EXIT_RUN_REFUSED, EXIT_USAGE

CONFIRMATION = "PURGE DROPIN"


def run(args) -> int:
    if args.purge_drop and not args.purge:
        print("dropin uninstall: --purge-drop requires --purge", file=sys.stderr)
        return EXIT_USAGE
    config_path = (Path(args.config).expanduser() if args.config
                   else default_config_path())
    try:
        targets = _targets(config_path, args)
    except (OSError, KeyError, TypeError, ValueError) as error:
        print(f"dropin uninstall: cannot read configuration: {error}", file=sys.stderr)
        return EXIT_USAGE

    if not args.purge:
        targets = {"binary": targets["binary"], "agent": targets["agent"]}

    unsafe = [path for path in targets.values()
              if path is not None and _unsafe(path)]
    if unsafe:
        print("dropin uninstall: refusing unsafe deletion path(s): "
              + ", ".join(map(str, unsafe)), file=sys.stderr)
        return EXIT_RUN_REFUSED

    print("Dropin will remove:")
    for name, path in targets.items():
        if path is not None:
            print(f"  {name}: {path}")
    if args.purge and not args.yes:
        if not sys.stdin.isatty():
            print("dropin uninstall: purge requires --yes in non-interactive mode",
                  file=sys.stderr)
            return EXIT_USAGE
        try:
            answer = input(f'Type {CONFIRMATION} to continue: ').strip()
        except (EOFError, KeyboardInterrupt):
            print("\ndropin uninstall: cancelled", file=sys.stderr)
            return EXIT_USAGE
        if answer != CONFIRMATION:
            print("dropin uninstall: confirmation did not match; nothing removed",
                  file=sys.stderr)
            return EXIT_USAGE

    if args.dry_run:
        print("dry run: nothing removed")
        return EXIT_OK

    # The plist can remain loaded after its file is deleted, so stop it first.
    agent = targets.get("agent")
    if agent and agent.exists() and sys.platform == "darwin":
        result = subprocess.run(
            ["launchctl", "bootout", f"gui/{os.getuid()}", str(agent)],
            text=True, capture_output=True)
        if result.returncode and "Could not find service" not in result.stderr:
            print(f"dropin uninstall: launchd bootout failed: {result.stderr.strip()}",
                  file=sys.stderr)
            return EXIT_RUN_REFUSED

    for name, path in targets.items():
        if path is None or (not path.exists() and not path.is_symlink()):
            continue
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
            print(f"removed {name}: {path}")
        except OSError as error:
            print(f"dropin uninstall: could not remove {path}: {error}", file=sys.stderr)
            return EXIT_RUN_REFUSED
    return EXIT_OK


def _targets(config_path: Path, args) -> dict[str, Path | None]:
    config_path = config_path.absolute()
    if config_path.is_symlink():
        raise ValueError("configuration path must not be a symlink")
    raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    paths = raw["paths"]
    repository = raw["repository"]
    launchd = raw.get("launchd", {})
    drop = _managed_path(paths["drop_dir"])
    state = _managed_path(paths["state_dir"])
    password = _managed_path(repository.get(
        "password_file", config_path.with_name("repo.password")))
    if drop == state or drop in state.parents or state in drop.parents:
        raise ValueError("drop and state directories must not overlap")
    label = launchd.get("label", "dev.dropin.drain")
    agent = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    binary = Path(args.binary).expanduser().absolute() if args.binary else _binary()
    result: dict[str, Path | None] = {
        "config": config_path, "password": password, "state": state,
        "agent": agent, "binary": binary,
    }
    if args.purge_drop:
        result["drop"] = drop
    return result


def _binary() -> Path | None:
    candidate = Path(sys.argv[0]).resolve()
    standalone_dir = (Path.home() / ".local" / "bin").resolve()
    if candidate.name == "dropin" and candidate.parent == standalone_dir and candidate.is_file():
        return candidate
    return None


def _managed_path(raw) -> Path:
    path = Path(os.path.expanduser(raw))
    if path.is_symlink():
        raise ValueError(f"managed path must not be a symlink: {path}")
    return Path(os.path.realpath(path))


def _unsafe(path: Path) -> bool:
    """Reject symlinks, filesystem roots, and the user's home directory."""
    if path.is_symlink():
        return True
    resolved = path.resolve()
    return resolved in (Path("/"), Path.home().resolve())
