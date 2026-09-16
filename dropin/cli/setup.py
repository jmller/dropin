"""Guided first-time setup for Dropin.

The actual initialization remains in :mod:`dropin.cli.init`; this module only
collects friendly defaults and answers so the safety rules have one owner.
"""

from __future__ import annotations

import sys
from pathlib import Path

from ..config import default_config_path
from ..report import EXIT_USAGE
from . import init

DEFAULT_DROP_DIR = "~/Drop"
DEFAULT_STATE_DIR = "~/.local/state/dropin"


def run(args) -> int:
    values = {
        "repo": args.repo,
        "drop_dir": args.drop_dir,
        "state_dir": args.state_dir,
    }
    missing = [name for name, value in values.items() if not value]
    if missing and not sys.stdin.isatty():
        names = ", ".join(f"--{name.replace('_', '-')}" for name in missing)
        print(f"dropin setup: interactive input required; provide {names}",
              file=sys.stderr)
        return EXIT_USAGE

    config_path = Path(args.config).expanduser() if args.config else default_config_path()
    if config_path.exists() and not args.force:
        if not sys.stdin.isatty():
            print(f"dropin setup: {config_path} exists; rerun interactively and type YES "
                  "to overwrite it, or use --force", file=sys.stderr)
            return EXIT_USAGE
        try:
            answer = _prompt(
                f"Configuration {config_path} already exists. "
                "Type YES to overwrite it: ")
        except (EOFError, KeyboardInterrupt):
            print("\ndropin setup: cancelled; no configuration was written",
                  file=sys.stderr)
            return EXIT_USAGE
        if answer != "YES":
            print("dropin setup: overwrite not confirmed; no configuration was written",
                  file=sys.stderr)
            return EXIT_USAGE
        args.force = True

    try:
        if not values["repo"]:
            values["repo"] = _prompt("Repository (for example, rclone:REMOTE:/archive): ")
        if not values["drop_dir"]:
            values["drop_dir"] = _prompt(
                f"Drop directory [{DEFAULT_DROP_DIR}]: ") or DEFAULT_DROP_DIR
        if not values["state_dir"]:
            values["state_dir"] = _prompt(
                f"State directory [{DEFAULT_STATE_DIR}]: ") or DEFAULT_STATE_DIR
    except (EOFError, KeyboardInterrupt):
        print("\ndropin setup: cancelled; no configuration was written",
              file=sys.stderr)
        return EXIT_USAGE

    if not values["repo"]:
        print("dropin setup: repository is required", file=sys.stderr)
        return EXIT_USAGE

    # init.run owns validation, secure password generation, repository setup,
    # and LaunchAgent rendering. Keep this namespace local
    # so setup can share that behavior without changing init's API.
    args.repo = values["repo"]
    args.drop_dir = values["drop_dir"]
    args.state_dir = values["state_dir"]
    return init.run(args)


def _prompt(message: str) -> str:
    return input(message).strip()
