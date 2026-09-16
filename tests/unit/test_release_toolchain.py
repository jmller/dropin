"""Release download metadata, checksum, platform, and exact-version gate."""

from __future__ import annotations

import bz2
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
METADATA = ROOT / "packaging" / "tool-checksums.json"


class ReleaseToolchainTest(unittest.TestCase):
    def module(self):
        from scripts.release_toolchain import (
            ToolchainError, load_metadata, platform_key, select_asset,
            verify_archive, verify_binary,
        )
        return ToolchainError, load_metadata, platform_key, select_asset, verify_archive, verify_binary

    def test_checked_metadata_has_supported_mac_and_linux_ci_assets(self):
        _, load, _, _, _, _ = self.module()
        data = load(METADATA)
        self.assertEqual(data["schema"], 1)
        self.assertEqual(data["tools"]["restic"]["version"], "0.19.1")
        self.assertEqual(data["tools"]["rclone"]["version"], "1.75.1")
        for tool in ("restic", "rclone"):
            with self.subTest(tool=tool):
                self.assertEqual(
                    set(data["tools"][tool]["assets"]),
                    {"darwin-arm64", "linux-amd64", "linux-arm64"},
                )
                self.assertTrue(
                    data["tools"][tool]["source_checksums_url"].startswith("https://"))
                for asset in data["tools"][tool]["assets"].values():
                    self.assertRegex(asset["sha256"], r"^[0-9a-f]{64}$")
                    self.assertTrue(asset["url"].startswith("https://"))
                    self.assertTrue(asset["url"].endswith(asset["filename"]))

    def test_platform_names_are_normalized_and_unsupported_values_refuse(self):
        Error, _, platform_key, _, _, _ = self.module()
        self.assertEqual(platform_key("Darwin", "arm64"), "darwin-arm64")
        self.assertEqual(platform_key("Linux", "x86_64"), "linux-amd64")
        self.assertEqual(platform_key("Linux", "aarch64"), "linux-arm64")
        for values in (("Darwin", "x86_64"), ("Windows", "AMD64")):
            with self.subTest(values=values), self.assertRaises(Error):
                platform_key(*values)

    def test_asset_selection_and_checksum_are_exact(self):
        Error, load, _, select, verify_archive, _ = self.module()
        data = load(METADATA)
        asset = select(data, "restic", "darwin-arm64")
        with tempfile.TemporaryDirectory(prefix="dropin-tool-") as raw:
            path = Path(raw) / asset["filename"]
            content = b"archive bytes"
            path.write_bytes(content)
            changed = dict(asset, sha256=sha256(content).hexdigest())
            verify_archive(path, changed)
            path.write_bytes(content + b"corrupt")
            with self.assertRaisesRegex(Error, "checksum"):
                verify_archive(path, changed)

    def test_wrong_archive_filename_refuses_even_with_matching_bytes(self):
        Error, _, _, _, verify_archive, _ = self.module()
        content = b"same"
        with tempfile.TemporaryDirectory(prefix="dropin-tool-") as raw:
            path = Path(raw) / "wrong.zip"
            path.write_bytes(content)
            with self.assertRaisesRegex(Error, "filename"):
                verify_archive(path, {
                    "filename": "expected.zip", "sha256": sha256(content).hexdigest()
                })

    def test_publisher_checksum_record_must_name_the_exact_asset_digest(self):
        from scripts.release_toolchain import verify_publisher_checksums
        Error, load, _, select, _, _ = self.module()
        data = load(METADATA)
        record = data["tools"]["restic"]
        asset = select(data, "restic", "darwin-arm64")
        with tempfile.TemporaryDirectory(prefix="dropin-tool-") as raw:
            sums = Path(raw) / "SHA256SUMS"
            sums.write_text(f"{asset['sha256']}  {asset['filename']}\n")
            verify_publisher_checksums(sums, record, asset)
            sums.write_text(f"{'0' * 64}  {asset['filename']}\n")
            with self.assertRaisesRegex(Error, "publisher checksum"):
                verify_publisher_checksums(sums, record, asset)

    def test_binary_must_match_archive_version_and_architecture(self):
        Error, load, _, _, _, verify_binary = self.module()
        data = load(METADATA)
        with tempfile.TemporaryDirectory(prefix="dropin-tool-") as raw:
            root = Path(raw)
            binary = root / "restic"
            binary.write_bytes(b"exact extracted binary")
            archive = root / "restic_0.19.1_darwin_arm64.bz2"
            archive.write_bytes(bz2.compress(binary.read_bytes()))
            good = subprocess.CompletedProcess(
                [str(binary), "version"], 0,
                stdout="restic 0.19.1 compiled with go on darwin/arm64\n", stderr="")
            with mock.patch("subprocess.run", return_value=good):
                verify_binary(binary, "restic", data, archive, "darwin-arm64")
            binary.write_bytes(b"different binary")
            with mock.patch("subprocess.run", return_value=good), \
                 self.assertRaisesRegex(Error, "does not match"):
                verify_binary(binary, "restic", data, archive, "darwin-arm64")
            binary.write_bytes(b"exact extracted binary")
            wrong_version = subprocess.CompletedProcess(
                [str(binary), "version"], 0,
                stdout="restic 0.20.0 compiled with go on darwin/arm64\n", stderr="")
            with mock.patch("subprocess.run", return_value=wrong_version), \
                 self.assertRaisesRegex(Error, "expected 0.19.1"):
                verify_binary(binary, "restic", data, archive, "darwin-arm64")
            wrong_arch = subprocess.CompletedProcess(
                [str(binary), "version"], 0,
                stdout="restic 0.19.1 compiled with go on linux/amd64\n", stderr="")
            with mock.patch("subprocess.run", return_value=wrong_arch), \
                 self.assertRaisesRegex(Error, "architecture"):
                verify_binary(binary, "restic", data, archive, "darwin-arm64")

    def test_malformed_or_unknown_metadata_fails_closed(self):
        Error, load, _, select, _, _ = self.module()
        with tempfile.TemporaryDirectory(prefix="dropin-tool-") as raw:
            path = Path(raw) / "metadata.json"
            path.write_text(json.dumps({"schema": 2, "tools": {}}))
            with self.assertRaises(Error):
                load(path)
            data = json.loads(METADATA.read_text())
            del data["tools"]["restic"]["source_checksums_url"]
            path.write_text(json.dumps(data))
            with self.assertRaises(Error):
                load(path)
        with self.assertRaises(Error):
            select(load(METADATA), "unknown", "darwin-arm64")


if __name__ == "__main__":
    unittest.main()
