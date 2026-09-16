#!/usr/bin/env python3
"""Fail-closed release verifier for pinned restic/rclone assets."""

from __future__ import annotations

import argparse
import bz2
from hashlib import sha256
import json
from pathlib import Path
import platform
import re
import subprocess
from typing import Any
import zipfile


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_METADATA = ROOT / "packaging" / "tool-checksums.json"
SUPPORTED_KEYS = frozenset({"darwin-arm64", "linux-amd64", "linux-arm64"})


class ToolchainError(Exception):
    """Release tool metadata or an observed asset is not exactly pinned."""


def load_metadata(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ToolchainError(f"cannot read tool metadata: {error}") from error
    if not isinstance(data, dict) or data.get("schema") != 1:
        raise ToolchainError("unsupported tool metadata schema")
    tools = data.get("tools")
    if not isinstance(tools, dict) or set(tools) != {"restic", "rclone"}:
        raise ToolchainError("tool metadata must define exactly restic and rclone")
    for name, record in tools.items():
        if not isinstance(record, dict) or not re.fullmatch(r"\d+\.\d+\.\d+", str(record.get("version", ""))):
            raise ToolchainError(f"invalid {name} version metadata")
        try:
            re.compile(record["version_pattern"])
            checksums_url = record["source_checksums_url"]
            assets = record["assets"]
        except (KeyError, TypeError, re.error) as error:
            raise ToolchainError(f"invalid {name} metadata: {error}") from error
        if (not isinstance(checksums_url, str)
                or not checksums_url.startswith("https://")
                or not checksums_url.endswith("SHA256SUMS")):
            raise ToolchainError(f"invalid {name} publisher checksum URL")
        if not isinstance(assets, dict) or set(assets) != SUPPORTED_KEYS:
            raise ToolchainError(f"invalid {name} platform set")
        for key, asset in assets.items():
            if not isinstance(asset, dict):
                raise ToolchainError(f"invalid {name} asset for {key}")
            filename = asset.get("filename")
            url = asset.get("url")
            digest = asset.get("sha256")
            if (not isinstance(filename, str) or Path(filename).name != filename
                    or not isinstance(url, str) or not url.startswith("https://")
                    or not url.endswith(filename)
                    or not isinstance(digest, str)
                    or re.fullmatch(r"[0-9a-f]{64}", digest) is None):
                raise ToolchainError(f"invalid {name} asset for {key}")
    return data


def platform_key(system: str, machine: str) -> str:
    systems = {"darwin": "darwin", "linux": "linux"}
    machines = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "amd64", "amd64": "amd64"}
    normalized_system = systems.get(system.lower())
    normalized_machine = machines.get(machine.lower())
    key = f"{normalized_system}-{normalized_machine}"
    if key not in SUPPORTED_KEYS:
        raise ToolchainError(f"unsupported release tool platform: {system}/{machine}")
    return key


def select_asset(data: dict[str, Any], tool: str, key: str) -> dict[str, str]:
    try:
        record = data["tools"][tool]
        asset = record["assets"][key]
    except KeyError as error:
        raise ToolchainError(f"no pinned {tool} asset for {key}") from error
    return dict(asset)


def verify_archive(path: Path, asset: dict[str, str]) -> None:
    if path.name != asset["filename"]:
        raise ToolchainError(
            f"archive filename {path.name!r} does not match {asset['filename']!r}")
    try:
        digest = sha256(path.read_bytes()).hexdigest()
    except OSError as error:
        raise ToolchainError(f"cannot read archive {path}: {error}") from error
    if digest != asset["sha256"]:
        raise ToolchainError(
            f"archive checksum mismatch: found {digest}, expected {asset['sha256']}")


def verify_publisher_checksums(path: Path, record: dict[str, Any],
                               asset: dict[str, str]) -> None:
    try:
        lines = path.read_text().splitlines()
    except (OSError, UnicodeError) as error:
        raise ToolchainError(f"cannot read publisher checksums: {error}") from error
    expected = asset["sha256"]
    found = []
    for line in lines:
        match = re.fullmatch(r"([0-9a-fA-F]{64})\s+\*?(.+)", line.strip())
        if match and Path(match.group(2)).name == asset["filename"]:
            found.append(match.group(1).lower())
    if found != [expected]:
        raise ToolchainError(
            f"publisher checksum does not uniquely match {asset['filename']}")


def _archived_binary(archive: Path, tool: str) -> bytes:
    try:
        if tool == "restic":
            return bz2.decompress(archive.read_bytes())
        with zipfile.ZipFile(archive) as bundle:
            members = [name for name in bundle.namelist()
                       if name.endswith(f"/{tool}") and not name.endswith("/")]
            if len(members) != 1:
                raise ToolchainError(
                    f"archive does not contain exactly one {tool} binary")
            return bundle.read(members[0])
    except (OSError, EOFError, ValueError, zipfile.BadZipFile) as error:
        raise ToolchainError(f"cannot extract {tool} binary: {error}") from error


def verify_binary(binary: Path, tool: str, data: dict[str, Any],
                  archive: Path, key: str) -> None:
    try:
        record = data["tools"][tool]
    except KeyError as error:
        raise ToolchainError(f"unknown tool: {tool}") from error
    try:
        observed = binary.read_bytes()
    except OSError as error:
        raise ToolchainError(f"cannot read {tool} binary {binary}: {error}") from error
    if observed != _archived_binary(archive, tool):
        raise ToolchainError(f"{tool} binary does not match the verified archive")
    try:
        result = subprocess.run(
            [str(binary), "version"], text=True, capture_output=True,
            timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ToolchainError(f"cannot run {tool} binary {binary}: {error}") from error
    if result.returncode != 0:
        raise ToolchainError(f"{tool} version exited {result.returncode}: {result.stderr.strip()}")
    match = re.search(record["version_pattern"], result.stdout, re.MULTILINE)
    found = match.group("version") if match else "unparseable"
    if found != record["version"]:
        raise ToolchainError(f"{tool} found {found}, expected {record['version']}")
    system, architecture = key.split("-", 1)
    platform_markers = ((f"{system}/{architecture}",)
                        if tool == "restic"
                        else (f"os/type: {system}", f"os/arch: {architecture}"))
    if not all(marker in result.stdout.lower() for marker in platform_markers):
        raise ToolchainError(
            f"{tool} version output does not confirm {key} architecture")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Verify one pinned release tool asset.")
    result.add_argument("--tool", choices=("restic", "rclone"), required=True)
    result.add_argument("--archive", type=Path, required=True)
    result.add_argument("--binary", type=Path, required=True)
    result.add_argument("--publisher-checksums", type=Path, required=True)
    result.add_argument("--metadata", type=Path, default=DEFAULT_METADATA)
    result.add_argument("--system", default=platform.system())
    result.add_argument("--machine", default=platform.machine())
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        data = load_metadata(args.metadata)
        key = platform_key(args.system, args.machine)
        asset = select_asset(data, args.tool, key)
        verify_archive(args.archive, asset)
        verify_publisher_checksums(
            args.publisher_checksums, data["tools"][args.tool], asset)
        verify_binary(args.binary, args.tool, data, args.archive, key)
    except ToolchainError as error:
        print(f"release-toolchain: {error}", file=__import__("sys").stderr)
        return 1
    print(f"verified {args.tool} {data['tools'][args.tool]['version']} for {key}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
