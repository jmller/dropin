"""Review regression: importer dates normalize without coercing free text."""
from contextlib import closing
from pathlib import Path
import tempfile
import unittest

from dropin.capture.extract import capture_item
from dropin.capture.mdimport_parser import MdimportParseError, parse_mdimport
from dropin.macos.fake import FakeMacOS
from dropin.store import records
from dropin.store.db import connect
from tests.unit.test_macos_recordings import recording


class ImporterDateTest(unittest.TestCase):
    def test_real_recording_dates_are_typed_iso_utc(self):
        attrs = parse_mdimport(recording("mdimport", "tagged.pdf-d3")["stdout"])
        for key in ("kMDItemContentCreationDate", "kMDItemContentModificationDate"):
            self.assertEqual(attrs[key].type, "date")
            self.assertEqual(attrs[key].value, "2026-09-08T09:20:40Z")

    def test_offset_converts_but_free_text_and_collections_stay_unchanged(self):
        raw = "2026-09-08 11:20:40 +0200"
        attrs = parse_mdimport('Attributes: { '
            f'kMDItemContentCreationDate = "{raw}"; '
            f'kMDItemTextContent = "{raw}"; '
            f'kMDItemUnknown = "{raw}"; '
            f'kMDItemKind = {{ "" = "{raw}"; }}; '
            f'kMDItemContentModificationDate = ("{raw}"); }}')
        self.assertEqual(attrs["kMDItemContentCreationDate"].value, "2026-09-08T09:20:40Z")
        for key in ("kMDItemTextContent", "kMDItemUnknown"):
            self.assertEqual((attrs[key].type, attrs[key].value), ("string", raw))
        self.assertEqual(attrs["kMDItemKind"].value, {"": raw})
        self.assertEqual(attrs["kMDItemContentModificationDate"].value, [raw])

    def test_invalid_calendar_date_is_parser_error(self):
        with self.assertRaises(MdimportParseError):
            parse_mdimport('Attributes: { kMDItemContentCreationDate = "2026-02-30 10:00:00 +0000"; }')

    def test_importer_only_capture_persists_canonical_dates(self):
        with tempfile.TemporaryDirectory(prefix="dropin-date-regression-") as temp:
            path = Path(temp) / "sample.pdf"
            path.write_bytes(b"disposable")
            macos = FakeMacOS()
            macos.set_importer(str(path), recording("mdimport", "tagged.pdf-d3")["stdout"])
            item = capture_item(macos, path)
            self.assertEqual(item.entries[0].capture_status, "partial:mdls")
            with closing(connect(Path(temp) / "store.sqlite")) as db:
                store = records.initialise_store(db)
                records.record_occurrence(db, item, store)
                self.assertEqual(tuple(db.execute("SELECT created,modified FROM normalized").fetchone()),
                                 ("2026-09-08T09:20:40Z", "2026-09-08T09:20:40Z"))
