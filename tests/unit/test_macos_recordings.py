"""Adapter regressions against real macOS recordings; not a macOS release gate."""
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest import mock

from dropin.capture.mdls_parser import Attr, MdlsParseError, parse_mdls
from dropin.capture.mdimport_parser import MdimportParseError, parse_mdimport
from dropin.macos.interface import CaptureFailure, Unsupported
from dropin.macos.real import RealMacOS

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def recording(family, name):
    return json.loads((FIXTURES / family / "recordings" / (name + ".json")).read_text())


class MdlsRecordingTest(unittest.TestCase):
    def test_pptx_metadata_and_importer_text(self):
        uti = "org.openxmlformats.presentationml.presentation"
        for family, parser in (("mdls", parse_mdls), ("mdimport", parse_mdimport)):
            with self.subTest(family=family):
                data = recording(family, "sample.pptx")
                self.assertEqual(data["returncode"], 0)
                self.assertEqual(data["stderr"], "")
                attributes = parser(data["stdout"])
                self.assertEqual(attributes["kMDItemContentType"], Attr(uti, "string"))
                tree = attributes["kMDItemContentTypeTree"].value
                self.assertIn(uti, tree)
                self.assertIn("public.presentation", tree)
                self.assertNotIn("com.apple.package", tree)
                self.assertNotIn("com.apple.bundle", tree)
                if family == "mdimport":
                    self.assertEqual(data["argv"][1:3], ["-d3", "-n"])
                    self.assertEqual(attributes["kMDItemTextContent"].value,
                                     "Dropin disposable PowerPoint evidence\n"
                                     "Generated sample only. No personal content.")

    def test_pdf_and_text_structures_preserve_raw_truncated_vector(self):
        for name, uti in (("tagged.pdf", "com.adobe.pdf"),
                          ("sample.txt", "public.plain-text")):
            with self.subTest(name=name):
                text = recording("mdls", name)["stdout"]
                attributes = parse_mdls(text)
                self.assertEqual(attributes["kMDItemContentType"], Attr(uti, "string"))
                vector = attributes["_kMDItemPrimaryTextEmbedding"]
                self.assertEqual(vector.type, "list")
                self.assertEqual(vector.value[0]["vec_dim"], 1)
                raw = vector.value[0]["vec_data"]
                self.assertIsInstance(raw, str)
                self.assertIn("length = 1024", raw)
                self.assertIn("...", raw)
                self.assertIn(raw, text)  # exact description, not invented vector bytes

    def test_app_and_folder_recordings(self):
        for name in ("Evidence.app", "plain-folder"):
            self.assertTrue(parse_mdls(recording("mdls", name)["stdout"]))

    def test_malformed_structures_are_not_bare_strings(self):
        for value in ('{ x = 1;', '("a" "b")', '{ x = ; }',
                      '{ x = 1; x = 2; }', '"ok" garbage',
                      '"bad\\U00zz"', '{length = 10, bytes = nope}',
                      '', ')', '(null) trailing', '"bad\\q"'):
            with self.subTest(value=value), self.assertRaises(MdlsParseError):
                parse_mdls('kMDItemTest = ' + value)


class MdimportRecordingTest(unittest.TestCase):
    def test_real_envelope_counts_and_all_keys_survive(self):
        for name, count in (("tagged.pdf", 33), ("tagged.pdf-d3", 31),
                            ("Evidence.app", 34), ("plain-folder", 26)):
            with self.subTest(name=name):
                attributes = parse_mdimport(recording("mdimport", name)["stdout"])
                self.assertEqual(len(attributes), count)
                self.assertEqual(attributes[":MD:DeviceId"].type, "number")
                self.assertIn("com_apple_metadata_modtime", attributes)
                self.assertEqual(attributes["kMDItemKind"].type, "dict")
        pdf = parse_mdimport(recording("mdimport", "tagged.pdf")["stdout"])
        self.assertEqual(pdf[":EA:_kMDItemUserTags"].value, ["Evidence\n6", "Disposable"])
        self.assertEqual(pdf["kMDItemKind"].value["ja"], "PDF書類")
        self.assertEqual(pdf["kMDItemKind"].value[""], "PDF document")
        self.assertEqual(len(pdf["kMDItemContentTypeTree"].value), 5)

    def test_d3_contains_full_text_not_d2_description(self):
        result = parse_mdimport(recording("mdimport", "tagged.pdf-d3")["stdout"])
        self.assertEqual(result["kMDItemTextContent"].value, "Dropin disposable PDF evidence.")

    def test_real_d3_bundle_and_no_text_folder(self):
        for name, uti in (("Evidence.app-d3", "com.apple.application-bundle"),
                          ("plain-folder-d3", "public.folder")):
            with self.subTest(name=name):
                data = recording("mdimport", name)
                self.assertEqual(data["argv"][1:3], ["-d3", "-n"])
                self.assertEqual(data["returncode"], 0)
                self.assertEqual(data["stderr"], "")
                attributes = parse_mdimport(data["stdout"])
                self.assertEqual(attributes["kMDItemContentType"], Attr(uti, "string"))
                self.assertNotIn("kMDItemTextContent", attributes)

    def test_nested_multiline_strings_escapes_and_unicode(self):
        text = r'''Attributes: {
            ":other:key" = { "" = ("a, b", "a\"b", "c\\d", "\U00e9", "\UD83D\UDE00"); };
            kMDItemTextContent = "line one
line two\nline three\tend";
        }'''
        result = parse_mdimport(text)
        self.assertEqual(result[":other:key"].value[""], ['a, b', 'a"b', 'c\\d', 'é', '😀'])
        self.assertEqual(result["kMDItemTextContent"].value, "line one\nline two\nline three\tend")

    def test_explicit_empty_dictionary_is_distinct_from_unknown_output(self):
        self.assertEqual(parse_mdimport("Attributes: {}"), {})
        self.assertEqual(parse_mdimport("Imported '/tmp/x' of type 'x' with no plugIn.\n0 attributes returned\n{}\n"), {})
        for text in ("", "unexpected diagnostics", "Imported '/tmp/x'\n", "{}",
                     "Attributes: { x = 1; } trailing", "Attributes: { x = ; }",
                     'Attributes: { x = ("a" "b"); }', 'Attributes: { x = "bad\\U123"; }',
                     "Attributes: { x = 1; x = 2; }", "Attributes: { x = 1 }",
                     "Imported '/tmp/x' of type 'x' with no plugIn.\n2 attributes returned\n{x = 1;}"):
            with self.subTest(text=text), self.assertRaises(MdimportParseError):
                parse_mdimport(text)


class RealMetadataAdapterTest(unittest.TestCase):
    def test_real_adapter_declares_validation_from_complete_recordings(self):
        self.assertEqual(RealMacOS.validation_state, "validated")

    def test_importer_uses_d3_and_parses_either_output_stream(self):
        text = recording("mdimport", "tagged.pdf-d3")["stdout"].encode()
        for stdout, stderr in ((text, b""), (b"", text)):
            with mock.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, stdout, stderr)) as run:
                result = RealMacOS().importer_attributes("/tmp/disposable.pdf")
            self.assertEqual(run.call_args.args[0], ["mdimport", "-d3", "-n", "/tmp/disposable.pdf"])
            self.assertEqual(result["kMDItemTextContent"].value, "Dropin disposable PDF evidence.")

    def test_parse_errors_are_capture_failures(self):
        for method in ("mdls", "importer_attributes"):
            with mock.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, b"unknown output", b"")):
                with self.subTest(method=method), self.assertRaises(CaptureFailure):
                    getattr(RealMacOS(), method)("/tmp/disposable")


class LsofRecordingTest(unittest.TestCase):
    def test_real_file_and_corrected_directory_controls(self):
        adapter = RealMacOS(platform="darwin")
        for condition in ("self-open", "self-closed", "tail-open", "tail-closed"):
            for form in ("file", "directory-corrected"):
                record = recording("ownership", condition + "-" + form)
                completed = subprocess.CompletedProcess([], record["returncode"], record["stdout"].encode(), record["stderr"].encode())
                with self.subTest(condition=condition, form=form), mock.patch("subprocess.run", return_value=completed):
                    self.assertEqual(adapter.open_descriptors("/tmp/probe", is_dir=form != "file"),
                                     [record["expected_pid"]] if condition.endswith("open") else [])

    def test_directory_option_immediately_precedes_absolute_operand(self):
        adapter = RealMacOS()
        self.assertEqual(adapter._argv("/tmp/probe", True), ["lsof", "-Fpn", "+D", "/tmp/probe"])
        self.assertEqual(adapter._argv("-relative", True), ["lsof", "-Fpn", "+D", os.path.abspath("-relative")])

    def test_unknown_or_orphan_fields_are_unsupported_not_clear(self):
        for stdout in (b"p12\nzunknown\n", b"f3\nn/tmp/x\n", b"n/tmp/x\n", b"pbad\n", b"p0\n", b"p12\nf\n", b"p12\nf???\n"):
            with self.subTest(stdout=stdout), mock.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, stdout, b"")):
                with self.assertRaises(Unsupported):
                    RealMacOS().open_descriptors("/tmp/x", False)

    def test_original_invalid_directory_command_still_refuses(self):
        record = recording("ownership", "self-open-directory")
        with mock.patch("subprocess.run", return_value=subprocess.CompletedProcess([], record["returncode"], record["stdout"].encode(), record["stderr"].encode())):
            with self.assertRaises(Unsupported):
                RealMacOS().open_descriptors("/tmp/x", True)

    def test_negative_controls_require_no_holders_not_just_no_self(self):
        pid = os.getpid()
        for closed in (([pid + 1], []), ([], [pid + 1])):
            with self.subTest(closed=closed), mock.patch.object(RealMacOS, "_lsof", side_effect=[[pid], [pid], *closed]):
                result = RealMacOS(platform="darwin").capabilities()
                self.assertFalse(result.ownership_check)
                self.assertIn("negative control", result.ownership_reason)
