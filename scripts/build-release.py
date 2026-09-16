#!/usr/bin/env python3
"""Build the canonical deterministic Dropin zipapp and checksum file."""

from __future__ import annotations

import argparse
import ast
from datetime import datetime, timezone
from hashlib import sha256
import os
from pathlib import Path, PurePosixPath
import stat
import tempfile
import zipfile


ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "packaging" / "release-files.txt"
LAUNCHER = b"from dropin.__main__ import main\nraise SystemExit(main())\n"
SHEBANG = b"#!/usr/bin/env python3\n"
ZIP_EPOCH = 315532800  # 1980-01-01, the earliest representable ZIP timestamp.


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Build deterministic Dropin release assets.")
    result.add_argument("--output-dir", type=Path, required=True)
    return result


def package_version() -> str:
    tree = ast.parse((ROOT / "dropin" / "__init__.py").read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id == "__version__"
                   for target in node.targets):
                value = ast.literal_eval(node.value)
                if isinstance(value, str):
                    return value
    raise SystemExit("dropin.__version__ must be one literal string")


def release_paths() -> list[tuple[str, Path]]:
    result: list[tuple[str, Path]] = []
    seen: set[str] = set()
    root = ROOT.resolve()
    for number, raw in enumerate(MANIFEST.read_text().splitlines(), 1):
        name = raw.strip()
        if not name or name.startswith("#"):
            continue
        pure = PurePosixPath(name)
        if pure.is_absolute() or ".." in pure.parts or name != pure.as_posix():
            raise SystemExit(f"unsafe release manifest path on line {number}: {raw!r}")
        if name in seen:
            raise SystemExit(f"duplicate release manifest path on line {number}: {name}")
        source = ROOT.joinpath(*pure.parts)
        try:
            resolved = source.resolve(strict=True)
            resolved.relative_to(root)
        except (FileNotFoundError, ValueError) as error:
            raise SystemExit(f"invalid release manifest path {name}: {error}") from error
        if source.is_symlink() or not resolved.is_file():
            raise SystemExit(f"release manifest entry is not a regular file: {name}")
        seen.add(name)
        result.append((name, resolved))
    if not result:
        raise SystemExit("release manifest is empty")
    return sorted(result)


def zip_timestamp() -> tuple[int, int, int, int, int, int]:
    raw = os.environ.get("SOURCE_DATE_EPOCH", str(ZIP_EPOCH))
    try:
        epoch = max(int(raw), ZIP_EPOCH)
    except ValueError as error:
        raise SystemExit("SOURCE_DATE_EPOCH must be an integer") from error
    value = datetime.fromtimestamp(epoch, timezone.utc)
    # ZIP timestamps have a two-second resolution.
    return value.year, value.month, value.day, value.hour, value.minute, value.second // 2 * 2


def info(name: str, timestamp: tuple[int, int, int, int, int, int]) -> zipfile.ZipInfo:
    result = zipfile.ZipInfo(name, timestamp)
    result.compress_type = zipfile.ZIP_DEFLATED
    result.create_system = 3
    result.external_attr = (stat.S_IFREG | 0o644) << 16
    return result


def build(output_dir: Path) -> tuple[Path, Path]:
    output_dir = output_dir.expanduser().absolute()
    output_dir.mkdir(parents=True, exist_ok=True)
    version = package_version()
    artifact = output_dir / f"dropin-{version}.pyz"
    checksum = output_dir / "SHA256SUMS"
    timestamp = zip_timestamp()

    with tempfile.TemporaryDirectory(prefix="dropin-release-", dir=output_dir) as raw:
        temporary = Path(raw) / artifact.name
        with temporary.open("wb") as stream:
            stream.write(SHEBANG)
        with zipfile.ZipFile(temporary, "a", compression=zipfile.ZIP_DEFLATED,
                             compresslevel=9, strict_timestamps=True) as archive:
            archive.writestr(info("__main__.py", timestamp), LAUNCHER, compresslevel=9)
            for name, source in release_paths():
                archive.writestr(info(name, timestamp), source.read_bytes(), compresslevel=9)
        temporary.chmod(0o755)
        os.replace(temporary, artifact)

    digest = sha256(artifact.read_bytes()).hexdigest()
    checksum.write_text(f"{digest}  {artifact.name}\n")
    return artifact, checksum


def main() -> int:
    args = parser().parse_args()
    artifact, checksum = build(args.output_dir)
    print(artifact)
    print(checksum)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
