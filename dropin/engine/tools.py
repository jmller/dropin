"""Startup tool gate: present, runnable, and at or above the pin.

Pinned versions are a dependency contract: below the pin the run
refuses with both the found and the required version, and nothing is attempted.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import shutil
import subprocess

from ..config import Config

VERSION_RE = {
    "restic": re.compile(r"^restic\s+v?(\d+)\.(\d+)(?:\.(\d+))?"),
    "rclone": re.compile(r"^rclone\s+v?(\d+)\.(\d+)(?:\.(\d+))?"),
}


class ToolGateError(Exception):
    """A required external tool is missing, unrunnable, or too old."""


@dataclass(frozen=True)
class ToolVersions:
    restic: tuple[int, int, int]
    rclone: tuple[int, int, int]
    restic_path: str
    rclone_path: str


def parse_version(tool: str, output: str) -> tuple[int, int, int]:
    for line in output.splitlines():
        match = VERSION_RE[tool].match(line.strip())
        if match:
            major, minor, patch = match.groups()
            return (int(major), int(minor), int(patch or 0))
    raise ToolGateError(f"cannot parse {tool} version from: {output.strip()!r}")


def parse_pin(pin: str) -> tuple[int, int, int]:
    parts = pin.split(".")
    if not 1 <= len(parts) <= 3 or not all(part.isdigit() for part in parts):
        raise ToolGateError(f"malformed version pin {pin!r}")
    padded = parts + ["0"] * (3 - len(parts))
    return tuple(int(part) for part in padded)  # type: ignore[return-value]


def check_tools(config: Config) -> ToolVersions:
    restic_path, restic_version = _probe("restic", config.restic, config)
    rclone_path, rclone_version = _probe("rclone", config.rclone, config)
    for tool, found, pin in (("restic", restic_version, config.restic_min),
                             ("rclone", rclone_version, config.rclone_min)):
        required = parse_pin(pin)
        if found < required:
            raise ToolGateError(
                f"{tool} {_render(found)} is below the required "
                f"{_render(required)} (found at "
                f"{restic_path if tool == 'restic' else rclone_path})")
    return ToolVersions(restic=restic_version, rclone=rclone_version,
                        restic_path=restic_path, rclone_path=rclone_path)


def _probe(tool: str, configured: str, config: Config) -> tuple[str, tuple[int, int, int]]:
    resolved = configured if "/" in configured else (shutil.which(configured) or configured)
    try:
        result = subprocess.run([resolved, "version"], capture_output=True,
                                timeout=config.timeout_seconds)
    except OSError as error:
        raise ToolGateError(f"{tool} not found at {resolved}: {error}") from error
    except subprocess.TimeoutExpired as error:
        raise ToolGateError(f"{tool} at {resolved} did not respond") from error
    if result.returncode != 0:
        raise ToolGateError(
            f"{tool} at {resolved} exited {result.returncode}: "
            f"{result.stderr.decode(errors='replace').strip()}")
    return resolved, parse_version(tool, result.stdout.decode(errors="replace"))


def _render(version: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in version)
