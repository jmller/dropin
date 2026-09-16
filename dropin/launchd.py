"""LaunchAgent plist generation.

`drain` is an invoked command; launchd is the only scheduler (no resident
daemon). `QueueDirectories` fires on arrival, `StartInterval` is the fallback
for a file that was still being written the first time. The plist is rendered
with `plistlib`, so a path with `&` or `<` in it is data, not markup.

Nothing here runs `launchctl`: `init --launchd` writes the file and prints the
bootstrap line, and the user runs it.
"""

from __future__ import annotations

import os
from pathlib import Path
import plistlib
import re
import sys

from .macos.interface import LaunchAgent

LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def render_plist(*, label: str, program, queue_directories, interval: int) -> str:
    if not LABEL_RE.match(label):
        raise ValueError(f"launchd label must be reverse-DNS-like: {label!r}")
    if interval <= 0:
        raise ValueError(f"launchd interval must be positive: {interval!r}")
    program = [str(item) for item in program]
    queues = [str(item) for item in queue_directories]
    if not program or not queues:
        raise ValueError("launchd agent needs a program and a queue directory")
    document = {
        "Label": label,
        "ProgramArguments": program,
        "QueueDirectories": queues,
        "StartInterval": int(interval),
        "RunAtLoad": False,
    }
    return plistlib.dumps(document, fmt=plistlib.FMT_XML,
                          sort_keys=False).decode("utf-8")


def program_arguments(config_path: Path | str) -> tuple[str, ...]:
    """The exact invocation launchd runs: this interpreter, this config."""
    return (sys.executable, "-m", "dropin", "--config", str(config_path), "drain")


def default_agent_path(label: str, home: Path | None = None) -> Path:
    home = Path.home() if home is None else Path(home)
    return home / "Library" / "LaunchAgents" / f"{label}.plist"


def bootstrap_command(agent_path: Path | str, uid: int | None = None) -> str:
    uid = os.getuid() if uid is None else uid
    return f"launchctl bootstrap gui/{uid} {agent_path}"


def bootout_command(agent_path: Path | str, uid: int | None = None) -> str:
    uid = os.getuid() if uid is None else uid
    return f"launchctl bootout gui/{uid} {agent_path}"


def install(macos, *, config_path: Path | str, drop_dir: Path | str, label: str,
            interval: int, agent_path: Path | str | None = None) -> LaunchAgent:
    """Write the agent through the platform seam and describe what was written."""
    agent_path = Path(agent_path) if agent_path else default_agent_path(label)
    program = program_arguments(config_path)
    queues = (str(drop_dir),)
    # Validate before touching the seam so a bad label never reaches disk.
    render_plist(label=label, program=program, queue_directories=queues,
                 interval=interval)
    macos.write_launch_agent(agent_path, label=label, program=program,
                             queue_directories=queues, interval=interval)
    return LaunchAgent(path=agent_path, label=label, program=program,
                       queue_directories=queues, interval=interval)
