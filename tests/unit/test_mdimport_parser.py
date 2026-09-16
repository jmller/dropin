"""Synthetic importer grammar/edge cases; real d3 output has separate tests."""

from __future__ import annotations

from pathlib import Path
import unittest

from dropin.capture.mdimport_parser import MdimportParseError, parse_mdimport

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic" / "mdimport"


def fixture(name: str) -> str:
    return (FIXTURES / f"{name}.txt").read_text()


class FixtureHygieneTest(unittest.TestCase):
    def test_synthetic_fixtures_are_marked(self):
        for path in FIXTURES.glob("*.txt"):
            with self.subTest(fixture=path.name):
                self.assertTrue(path.with_suffix(".synthetic").exists())


class ImporterDictionaryTest(unittest.TestCase):
    def setUp(self):
        self.attributes = parse_mdimport(fixture("pdf_with_text"))

    def test_text_content_is_extracted(self):
        value = self.attributes["kMDItemTextContent"]
        self.assertEqual(value.value, "quarterly results, revenue up")
        self.assertEqual(value.type, "string")

    def test_numbers_are_typed(self):
        value = self.attributes["kMDItemNumberOfPages"]
        self.assertEqual(value.value, 3)
        self.assertEqual(value.type, "number")

    def test_lists_are_typed(self):
        value = self.attributes["kMDItemAuthors"]
        self.assertEqual(value.value, ["A. Author", "B. Author"])
        self.assertEqual(value.type, "list")

    def test_importer_only_key_is_retained(self):
        # The key does not appear in mdls output; a fixed schema would drop it.
        self.assertEqual(self.attributes["kMDItemImporterOnlyKey"].value,
                         "importer-supplied")

    def test_key_overlapping_mdls_keeps_the_importer_value(self):
        # Merge policy lives in capture/extract.py; the parser reports what it saw.
        self.assertEqual(self.attributes["kMDItemKind"].value, "PDF Document")

    def test_diagnostic_preamble_is_ignored(self):
        self.assertNotIn("Import", self.attributes)
        for key in self.attributes:
            self.assertTrue(key.startswith("kMDItem"), key)


class NoTextTest(unittest.TestCase):
    def test_importer_without_text_yields_a_dictionary_without_it(self):
        attributes = parse_mdimport(fixture("no_text"))
        self.assertNotIn("kMDItemTextContent", attributes)
        self.assertEqual(attributes["kMDItemPixelHeight"].value, 1024)

    def test_bundle_importer_supplies_text_at_the_root(self):
        attributes = parse_mdimport(fixture("bundle"))
        self.assertEqual(attributes["kMDItemTextContent"].value,
                         "dear sir or madam")


class MalformedTest(unittest.TestCase):
    def test_malformed_entry_names_the_line(self):
        with self.assertRaises(MdimportParseError) as caught:
            parse_mdimport(fixture("malformed"))
        self.assertIn("kMDItemBroken", str(caught.exception))

    def test_missing_attributes_block_is_an_empty_dictionary(self):
        self.assertEqual(parse_mdimport("mdimport: no importer for this type\n"),
                         {})

    def test_unterminated_block_is_an_error(self):
        with self.assertRaises(MdimportParseError):
            parse_mdimport('Attributes: {\n    "kMDItemKind" = "x";\n')
