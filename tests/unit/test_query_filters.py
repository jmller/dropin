"""Exact filter sets, boundaries and invalid input."""

from dropin.query.filters import Filters, QueryError, date_bound
from dropin.query.search import find
from tests.query_support import QueryTestCase


class QueryFiltersTest(QueryTestCase):
    def names(self, **kwargs):
        return {r["name"] for r in find(self.db, Filters(**kwargs))}

    def test_each_filter_returns_exact_known_set(self):
        cases = [
            ({"name": "TAX"}, {"Tax-March.PDF", "tax-april.pdf", "tax-old.pdf"}),
            ({"glob": "tax-*.pdf"}, {"tax-april.pdf", "tax-old.pdf"}),
            ({"glob": "[!t]*.pdf"}, {"receipt.pdf", "draft.pdf"}),
            ({"uti": "com.adobe.pdf"}, {"Tax-March.PDF", "tax-april.pdf", "receipt.pdf", "tax-old.pdf", "draft.pdf"}),
            ({"kind": "com.adobe.pdf"}, {"Tax-March.PDF", "tax-april.pdf", "receipt.pdf", "tax-old.pdf", "draft.pdf"}),
            ({"created_since": "2026-04-01"}, {"receipt.pdf", "photo.jpg", "Résumé.txt", "draft.pdf"}),
            ({"created_until": "2026-03-01"}, {"Tax-March.PDF", "letter.txt", "budget.csv", "100%_literal.txt"}),
            ({"modified_since": "2026-04-03"}, {"tax-april.pdf", "letter.txt", "photo.jpg", "tax-old.pdf", "100%_literal.txt", "draft.pdf"}),
            ({"modified_until": "2026-04-02T12:00:00Z"}, {"Tax-March.PDF", "receipt.pdf", "Notes.txt", "budget.csv", "Résumé.txt", "empty.txt"}),
            ({"tags": ["tax"]}, {"Tax-March.PDF", "receipt.pdf", "Notes.txt", "budget.csv", "Résumé.txt", "empty.txt"}),
            ({"size_min": 100}, {"empty.txt", "draft.pdf"}),
            ({"size_max": 10}, {"Tax-March.PDF", "tax-april.pdf"}),
            ({"sha256": self.hashes["Notes.txt"]}, {"Notes.txt"}),
            ({"text": "orchid"}, {"Tax-March.PDF", "receipt.pdf", "photo.jpg", "Notes.txt"}),
            ({"text": 'name:Résumé'}, {"Résumé.txt"}),
        ]
        for filters, expected in cases:
            with self.subTest(filters=filters):
                self.assertEqual(self.names(**filters), expected)

    def test_all_filters_and_repeated_tags_are_and_combined(self):
        self.assertEqual(self.names(name="Tax", kind="com.adobe.pdf", tags=["tax", "work"],
                                    created_since="2026-03-01", created_until="2026-03-31",
                                    modified_since="2026-04-02", modified_until="2026-04-02",
                                    size_min=0, size_max=0, text="invoice",
                                    sha256=self.hashes["Tax-March.PDF"]), {"Tax-March.PDF"})
        self.assertEqual(self.names(tags=["tax", "work"]), {"Tax-March.PDF", "budget.csv"})
        self.assertEqual(self.names(tags=["Tax"]), set())

    def test_date_only_until_includes_fractional_last_second_not_next_midnight(self):
        self.assertEqual(self.names(created_since="2026-03-31", created_until="2026-03-31"),
                         {"tax-april.pdf", "Notes.txt", "tax-old.pdf", "empty.txt"})

    def test_timestamp_bounds_are_inclusive_and_offsets_normalize_to_utc(self):
        self.assertEqual(self.names(created_since="2026-03-01T01:00:00+01:00",
                                    created_until="2026-03-01T00:00:00"),
                         {"Tax-March.PDF", "letter.txt", "budget.csv", "100%_literal.txt"})
        self.assertEqual(self.names(created_since="2026-03-31T23:59:59.999999Z",
                                    created_until="2026-03-31T23:59:59.999999Z"),
                         {"tax-april.pdf", "Notes.txt", "tax-old.pdf", "empty.txt"})

    def test_timezone_offset_components_are_validated_before_conversion(self):
        for offset in ("+00:60", "-00:60", "+01:99", "-01:99", "+24:00", "-24:00"):
            for field in ("created_since", "created_until", "modified_since", "modified_until"):
                with self.subTest(offset=offset, field=field), self.assertRaises(QueryError):
                    Filters(**{field: "2026-03-01T00:00:00" + offset})
        for offset, expected in (("+00:59", "2026-02-28T23:01:00.000000+00:00"),
                                 ("-00:59", "2026-03-01T00:59:00.000000+00:00"),
                                 ("+23:59", "2026-02-28T00:01:00.000000+00:00"),
                                 ("-23:59", "2026-03-01T23:59:00.000000+00:00")):
            with self.subTest(offset=offset):
                self.assertEqual(date_bound("2026-03-01T00:00:00" + offset), (expected, False))

    def test_name_is_unicode_casefolded_literal_not_sql_pattern(self):
        self.assertEqual(self.names(name="RÉSUMÉ"), {"Résumé.txt"})
        self.assertEqual(self.names(name="%_"), {"100%_literal.txt"})
        self.assertEqual(self.names(name="' OR 1=1 --"), set())

    def test_fts_uses_committed_normalized_external_content(self):
        self.db.execute("UPDATE normalized SET text='amaryllis' WHERE name='empty.txt'")
        self.assertEqual(self.names(text="amaryllis"), {"empty.txt"})
        self.assertEqual(self.names(text="orchid AND invoice"), {"Tax-March.PDF", "receipt.pdf", "photo.jpg"})

    def test_limit_and_order_are_deterministic(self):
        expected = sorted(self.paths.values())[:3]
        self.assertEqual([r["archive_path"] for r in find(self.db, Filters(limit=3))], expected)
        self.assertEqual(Filters().limit, 100)
        occ = self.seed("newest.txt")
        self.db.execute("UPDATE occurrence SET recorded_at='2026-06-01T00:00:00Z' WHERE occ_id=?", (occ,))
        self.assertEqual(next(iter(find(self.db, Filters(limit=1))))["name"], "newest.txt")

    def test_invalid_filters_fail_not_silent_empty(self):
        for kwargs in ({"limit": 0}, {"limit": True}, {"limit": 1.5}, {"size_min": -1},
                       {"size_max": 2**63}, {"size_min": 3, "size_max": 2}, {"sha256": "bad"},
                       {"created_since": "2026-02-30"}, {"created_until": "not a date"},
                       {"created_since": "2026-04-02", "created_until": "2026-04-01"},
                       {"name": "a", "glob": "*"}, {"kind": "x", "uti": "x"},
                       {"tags": "tax"}, {"tags": [1]}, {"name": 1}, {"text": '"unfinished'}):
            with self.subTest(kwargs=kwargs), self.assertRaises(QueryError):
                list(find(self.db, Filters(**kwargs)))
