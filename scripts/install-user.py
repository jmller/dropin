#!/usr/bin/env python3
"""Install Dropin as a pip-free user command.

The installed zipapp contains the Python package and schema resources. External
restic/rclone binaries and the user's configuration remain outside the archive.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import runpy
import shutil
import stat
import tempfile


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Install the dropin command without pip.")
    result.add_argument(
        "--target",
        type=Path,
        default=Path.home() / ".local" / "bin" / "dropin",
        help="installed executable (default: ~/.local/bin/dropin)",
    )
    result.add_argument(
        "--artifact",
        type=Path,
        help="install this canonical release zipapp instead of building from the checkout",
    )
    return result


def install(target: Path, artifact: Path | None = None) -> None:
    target = target.expanduser().absolute()
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="dropin-install-", dir=target.parent) as raw:
        temporary_root = Path(raw)
        if artifact is None:
            builder = Path(__file__).resolve().with_name("build-release.py")
            if not builder.is_file():
                raise SystemExit("canonical release builder is unavailable")
            build = runpy.run_path(str(builder))["build"]
            source, _checksum = build(temporary_root / "assets")
        else:
            source = artifact.expanduser().absolute()
            if not source.is_file() or source.is_symlink():
                raise SystemExit(f"release artifact is not a regular file: {source}")
        candidate = temporary_root / "dropin"
        shutil.copyfile(source, candidate)
        candidate.chmod(candidate.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        os.replace(candidate, target)

    print(f"Installed dropin at {target}")
    if target.parent not in (Path(part) for part in os.environ.get("PATH", "").split(os.pathsep)):
        print(f'Add {target.parent} to PATH, for example: export PATH="{target.parent}:$PATH"')
    print("MCP command: dropin --config /absolute/path/to/config.toml mcp")


def main() -> int:
    args = parser().parse_args()
    install(args.target, args.artifact)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
