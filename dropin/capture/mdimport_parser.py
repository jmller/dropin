"""`mdimport -d3 -n` descriptions → full typed importer attributes.

Accept the recorded Imported/count/dictionary envelope and the older synthetic
Attributes envelope. Unknown or truncated diagnostics are errors, not evidence
that an importer supplied nothing. d2 recordings parse too, but their text is a
description rather than the full extracted content; the real adapter uses d3.
"""
from __future__ import annotations

import re

from .mdls_parser import Attr, DATE_RE, MdlsParseError, _ValueReader, _scalar

# Real importer dates are quoted. Only recognized top-level date fields are
# coerced: arbitrary text and nested localized strings must remain strings.
DATE_KEYS = frozenset({
    "kMDItemContentCreationDate", "kMDItemContentModificationDate",
    "kMDItemDateAdded", "kMDItemLastUsedDate", "kMDItemDownloadedDate",
    "kMDItemFSCreationDate", "kMDItemFSContentChangeDate",
    "_kMDItemCreationDate", "_kMDItemContentChangeDate",
})

REAL_ENVELOPE = re.compile(
    r"Imported '[^\n]+' of type '[^\n]+' with (?:no plugIn|plugIn [^\n]+)\.\n"
    r"(\d+) attributes returned\s*")
SYNTHETIC_ENVELOPE = re.compile(
    r"(?:\d{4}-\d\d-\d\d [^\n]+ mdimport\[\d+:\d+\] Import [^\n]+\n)?"
    r"Attributes:\s*")


class MdimportParseError(ValueError):
    """Malformed or unknown importer output; includes source context."""


def parse_mdimport(text: str) -> dict[str, Attr]:
    text = text.strip()
    # Retained explicit no-importer fixture outcome, not a catch-all for errors.
    if text in ("mdimport: no importer", "mdimport: no importer for this type"):
        return {}
    envelope = REAL_ENVELOPE.match(text)
    count = int(envelope.group(1)) if envelope else None
    if envelope is None:
        envelope = SYNTHETIC_ENVELOPE.match(text)
    if envelope is None:
        raise MdimportParseError(f"unknown mdimport envelope: {text[:160]!r}")
    reader = _ValueReader(text)
    reader.index = envelope.end()
    try:
        attributes = reader.dictionary()
        if reader.skip_space():
            reader.fail("trailing mdimport output")
        for key in DATE_KEYS & attributes.keys():
            attr = attributes[key]
            if attr.type == "string" and DATE_RE.fullmatch(attr.value):
                attributes[key] = _scalar(attr.value)
    except MdlsParseError as error:
        raise MdimportParseError(str(error)) from error
    if count is not None and count != len(attributes):
        raise MdimportParseError(f"mdimport reported {count} attributes, parsed {len(attributes)}")
    return attributes
