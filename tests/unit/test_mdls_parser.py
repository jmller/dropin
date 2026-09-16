"""`mdls` output → typed attribute dictionary.

Artificial edge cases explicitly use `tests/fixtures/synthetic/mdls/` and keep
`.synthetic` markers. Real output is covered by recording/provenance tests.
"""

from __future__ import annotations

from pathlib import Path
import unittest

from dropin.capture.mdls_parser import MdlsParseError, parse_mdls

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic" / "mdls"


def fixture(name: str) -> str:
    return (FIXTURES / f"{name}.txt").read_text()


class FixtureHygieneTest(unittest.TestCase):
    def test_synthetic_fixtures_are_marked(self):
        for path in FIXTURES.glob("*.txt"):
            with self.subTest(fixture=path.name):
                self.assertTrue(path.with_suffix(".synthetic").exists(),
                                "an unmarked fixture claims to be a real recording")


class ScalarTest(unittest.TestCase):
    def setUp(self):
        self.attributes = parse_mdls(fixture("pdf_tagged"))

    def test_quoted_string(self):
        value = self.attributes["kMDItemContentType"]
        self.assertEqual(value.value, "com.adobe.pdf")
        self.assertEqual(value.type, "string")

    def test_embedded_quotes_are_unescaped(self):
        self.assertEqual(self.attributes["kMDItemFinderComment"].value,
                         'quarterly "final" copy')

    def test_string_with_a_comma_is_one_value(self):
        self.assertEqual(self.attributes["kMDItemDisplayName"].value,
                         "report, final.pdf")

    def test_integer(self):
        value = self.attributes["kMDItemFSSize"]
        self.assertEqual(value.value, 12345)
        self.assertEqual(value.type, "number")

    def test_date_becomes_iso_utc(self):
        value = self.attributes["kMDItemContentCreationDate"]
        self.assertEqual(value.value, "2026-03-01T10:11:12Z")
        self.assertEqual(value.type, "date")

    def test_null_is_typed_null(self):
        value = self.attributes["kMDItemWhereFroms"]
        self.assertIsNone(value.value)
        self.assertEqual(value.type, "null")

    def test_zero_stays_a_number_not_a_bool(self):
        value = self.attributes["kMDItemFSIsStationery"]
        self.assertEqual(value.value, 0)
        self.assertEqual(value.type, "number")


class ListTest(unittest.TestCase):
    def setUp(self):
        self.attributes = parse_mdls(fixture("pdf_tagged"))

    def test_multiline_list(self):
        value = self.attributes["kMDItemContentTypeTree"]
        self.assertEqual(value.value,
                         ["com.adobe.pdf", "public.data", "public.item"])
        self.assertEqual(value.type, "list")

    def test_list_items_may_be_unquoted(self):
        self.assertIn("tax", self.attributes["kMDItemUserTags"].value)

    def test_list_item_containing_a_comma(self):
        self.assertEqual(self.attributes["kMDItemUserTags"].value,
                         ["tax", "important, urgent"])


class UnknownKeyTest(unittest.TestCase):
    def test_unknown_keys_are_retained(self):
        # No fixed schema: a key we have never seen must survive.
        attributes = parse_mdls(
            'kMDItemSomethingNobodyPlannedFor = "kept"\n')
        self.assertEqual(attributes["kMDItemSomethingNobodyPlannedFor"].value,
                         "kept")

    def test_every_fixture_key_round_trips(self):
        for name in ("pdf_tagged", "text_plain", "pages_bundle", "app_bundle",
                     "folder_plain"):
            with self.subTest(fixture=name):
                attributes = parse_mdls(fixture(name))
                self.assertTrue(attributes)
                for key in attributes:
                    self.assertTrue(key.startswith("kMDItem"), key)


class MalformedTest(unittest.TestCase):
    def test_malformed_line_names_the_line(self):
        with self.assertRaises(MdlsParseError) as caught:
            parse_mdls(fixture("malformed"))
        self.assertIn("this line has no equals sign at all",
                      str(caught.exception))

    def test_unterminated_list_is_an_error(self):
        with self.assertRaises(MdlsParseError):
            parse_mdls('kMDItemContentTypeTree = (\n    "public.data",\n')

    def test_empty_output_is_an_empty_dictionary(self):
        self.assertEqual(parse_mdls(""), {})


class TypeSpecificFixtureTest(unittest.TestCase):
    def test_text_fixture_exposes_searchable_text(self):
        attributes = parse_mdls(fixture("text_plain"))
        self.assertEqual(attributes["kMDItemTextContent"].value,
                         "alpha beta gamma")

    def test_folder_fixture_is_not_a_package(self):
        tree = parse_mdls(fixture("folder_plain"))["kMDItemContentTypeTree"].value
        self.assertNotIn("com.apple.package", tree)
        self.assertNotIn("com.apple.bundle", tree)

    def test_pages_fixture_is_a_package(self):
        tree = parse_mdls(fixture("pages_bundle"))["kMDItemContentTypeTree"].value
        self.assertIn("com.apple.package", tree)

    def test_app_fixture_is_a_bundle(self):
        tree = parse_mdls(fixture("app_bundle"))["kMDItemContentTypeTree"].value
        self.assertIn("com.apple.bundle", tree)
