"""Real-fixture provenance and metadata replay, not live Mac release tests."""
import json
from pathlib import Path
import tempfile
import unittest

from dropin.capture.extract import capture_item
from dropin.macos.fake import FakeMacOS

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


class FixtureProvenanceTest(unittest.TestCase):
    def test_default_fixtures_exactly_match_recorded_sources(self):
        manifest = json.loads((FIXTURES / "provenance.json").read_text())
        files = {str(path.relative_to(FIXTURES))
                 for family in ("mdls", "mdimport", "xattr", "ownership")
                 for path in (FIXTURES / family).iterdir()
                 if path.is_file() and path.name != "__init__.py"}
        self.assertEqual(files, set(manifest))
        for name, source in manifest.items():
            with self.subTest(fixture=name):
                raw = (FIXTURES / source["recording"]).read_bytes()
                record = json.loads(raw)
                transform = source["transform"]
                if transform == "stdout":
                    self.assertEqual(record["returncode"], 0)
                    self.assertEqual(record["stderr"], "")
                    expected = record["stdout"].encode()
                elif transform == "hex-stdout":
                    self.assertEqual(record["returncode"], 0)
                    self.assertEqual(record["stderr"], "")
                    expected = bytes.fromhex(record["stdout"])
                else:
                    self.assertEqual(transform, "identity")
                    expected = raw
                self.assertEqual((FIXTURES / name).read_bytes(), expected)
                self.assertFalse((FIXTURES / name).with_suffix(".synthetic").exists())

    def test_synthetic_edges_remain_explicitly_marked(self):
        synthetic = FIXTURES / "synthetic"
        files = [p for p in synthetic.rglob("*") if p.suffix in (".txt", ".json", ".bin")]
        self.assertTrue(files)
        for path in files:
            with self.subTest(fixture=str(path.relative_to(synthetic))):
                self.assertTrue(path.with_suffix(".synthetic").exists())
        self.assertTrue((synthetic / "mdls/pages_bundle.synthetic").exists())
        self.assertFalse((FIXTURES / "mdls/pages_bundle.txt").exists())

    def test_real_metadata_replays_through_capture(self):
        cases = (
            ("pdf_tagged", "pdf_with_text", "com.adobe.pdf", "Dropin disposable PDF evidence."),
            ("pptx_presentation", "pptx_with_text", "org.openxmlformats.presentationml.presentation",
             "Dropin disposable PowerPoint evidence\nGenerated sample only. No personal content."),
        )
        with tempfile.TemporaryDirectory() as temp:
            for mdls, importer, uti, text in cases:
                with self.subTest(sample=mdls):
                    path = Path(temp) / mdls
                    path.write_bytes(b"metadata replay payload, not the original document")
                    macos = FakeMacOS()
                    macos.set_mdls(str(path), (FIXTURES / "mdls" / (mdls + ".txt")).read_text())
                    macos.set_importer(str(path), (FIXTURES / "mdimport" / (importer + ".txt")).read_text())
                    if mdls == "pdf_tagged":
                        for key, file in (("com.apple.metadata:_kMDItemUserTags", "tags_plist.bin"),
                                          ("com.apple.metadata:kMDItemFinderComment", "comment_plist.bin")):
                            macos.set_xattr(str(path), key, (FIXTURES / "xattr" / file).read_bytes())
                    item = capture_item(macos, path)
                    self.assertEqual(item.kind, "file")
                    entry = item.entries[0]
                    self.assertEqual(entry.capture_status, "ok")
                    self.assertEqual(entry.text, text)
                    self.assertEqual(entry.attributes[("kMDItemContentType", "mdls")].value, uti)
                    self.assertEqual(entry.attributes[("kMDItemContentType", "importer")].value, uti)
                    if mdls == "pdf_tagged":
                        self.assertEqual(entry.tags, ["Evidence", "Disposable"])
                        self.assertEqual(entry.comment, "Disposable evidence only")


if __name__ == "__main__":
    unittest.main()
