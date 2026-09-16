"""Spotlight text descriptions → typed attributes, retaining unknown keys.

The shared value reader handles the observed OpenStep-style lists/dictionaries,
not arbitrary property lists. NSData's abbreviated `{length…, bytes…}` debug
representation is retained verbatim as a string: it is NOT the vector's bytes.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re

DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) ([+-]\d{4})$")
KEY_RE = re.compile(r"(_?kMDItem\w+)[ \t]*=[ \t]*")
DATA_RE = re.compile(
    r"\{length\s*=\s*\d+,\s*bytes\s*=\s*0x[0-9a-fA-F]+"
    r"(?:\s+(?:[0-9a-fA-F]+|\.\.\.))*\s*\}")


class MdlsParseError(ValueError):
    """Malformed or unsupported Spotlight description; includes source context."""


@dataclass(frozen=True)
class Attr:
    value: object
    type: str


def parse_mdls(text: str) -> dict[str, Attr]:
    reader = _ValueReader(text)
    attributes: dict[str, Attr] = {}
    while reader.skip_space():
        match = KEY_RE.match(text, reader.index)
        if not match:
            reader.fail("unparseable mdls line")
        key = match.group(1)
        if key in attributes:
            reader.fail(f"duplicate mdls key {key!r}")
        reader.index = match.end()
        # A missing value must not consume the next attribute as a bare string.
        if reader.index == len(text) or text[reader.index] in "\r\n":
            reader.fail(f"mdls entry has no value: {key}")
        attributes[key] = reader.value()
        while reader.index < len(text) and text[reader.index] in " \t":
            reader.index += 1
        if reader.index < len(text) and text[reader.index] not in "\r\n":
            reader.fail("trailing mdls value data")
    return attributes


class _ValueReader:
    """Cursor reader shared by mdls and mdimport; rejects partial structures."""
    def __init__(self, text: str):
        self.text = text
        self.index = 0

    def fail(self, message: str):
        start = self.text.rfind("\n", 0, self.index) + 1
        end = self.text.find("\n", self.index)
        line = self.text[start:end if end != -1 else len(self.text)]
        raise MdlsParseError(f"{message} at offset {self.index}: {line!r}")

    def skip_space(self) -> bool:
        while self.index < len(self.text) and self.text[self.index].isspace():
            self.index += 1
        return self.index < len(self.text)

    def take(self, token: str) -> bool:
        self.skip_space()
        if self.text.startswith(token, self.index):
            self.index += len(token)
            return True
        return False

    def require(self, token: str):
        if not self.take(token):
            self.fail(f"expected {token!r}")

    def value(self, depth: int = 0) -> Attr:
        if depth > 64:
            self.fail("Spotlight structure nesting exceeds 64")
        if not self.skip_space():
            self.fail("missing value")
        char = self.text[self.index]
        if char == '"':
            return Attr(self.quoted(), "string")
        if self.text.startswith("(null)", self.index):
            self.index += len("(null)")
            return Attr(None, "null")
        if char == "(":
            self.index += 1
            items = []
            if self.take(")"):
                return Attr(items, "list")
            while True:
                items.append(self.value(depth + 1).value)
                if self.take(")"):
                    return Attr(items, "list")
                self.require(",")
                if self.take(")"):
                    return Attr(items, "list")
        if char == "{":
            data = DATA_RE.match(self.text, self.index)
            if data:
                self.index = data.end()
                return Attr(data.group(), "string")
            return Attr({key: attr.value for key, attr in self.dictionary(depth).items()}, "dict")
        start = self.index
        while self.index < len(self.text) and self.text[self.index] not in ',;(){}=\r\n"':
            self.index += 1
        raw = self.text[start:self.index].strip()
        if not raw:
            self.fail("missing or malformed value")
        return _scalar(raw)

    def dictionary(self, depth: int = 0) -> dict[str, Attr]:
        self.require("{")
        result = {}
        while not self.take("}"):
            if not self.skip_space():
                self.fail("unterminated dictionary")
            if self.text[self.index] == '"':
                key = self.quoted()
            else:
                match = re.match(r"[\w:./-]+", self.text[self.index:])
                if not match:
                    self.fail("malformed dictionary key")
                key = match.group()
                self.index += len(key)
            if key in result:
                self.fail(f"duplicate dictionary key {key!r}")
            self.require("=")
            result[key] = self.value(depth + 1)
            self.require(";")
        return result

    def quoted(self) -> str:
        self.index += 1
        out = []
        escapes = {'"': '"', "\\": "\\", "n": "\n", "r": "\r", "t": "\t",
                   "b": "\b", "f": "\f"}
        while self.index < len(self.text):
            char = self.text[self.index]
            self.index += 1
            if char == '"':
                return "".join(out)
            if char != "\\":
                out.append(char)
                continue
            if self.index == len(self.text):
                self.fail("unterminated escape")
            escape = self.text[self.index]
            self.index += 1
            if escape == "U":
                code = self.unicode_unit()
                if 0xD800 <= code <= 0xDBFF:
                    if not self.text.startswith("\\U", self.index):
                        self.fail("unpaired Unicode high surrogate")
                    self.index += 2
                    low = self.unicode_unit()
                    if not 0xDC00 <= low <= 0xDFFF:
                        self.fail("invalid Unicode surrogate pair")
                    code = 0x10000 + ((code - 0xD800) << 10) + low - 0xDC00
                elif 0xDC00 <= code <= 0xDFFF:
                    self.fail("unpaired Unicode low surrogate")
                out.append(chr(code))
            elif escape in escapes:
                out.append(escapes[escape])
            else:
                self.fail(f"unsupported escape \\{escape}")
        self.fail("unterminated quoted value")

    def unicode_unit(self) -> int:
        raw = self.text[self.index:self.index + 4]
        if not re.fullmatch(r"[0-9a-fA-F]{4}", raw):
            self.fail("malformed Unicode escape")
        self.index += 4
        return int(raw, 16)


def _scalar(raw: str) -> Attr:
    if raw == "null":
        return Attr(None, "null")
    date = DATE_RE.fullmatch(raw)
    if date:
        try:
            stamp = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S %z")
        except ValueError as error:
            raise MdlsParseError(f"invalid date {raw!r}") from error
        return Attr(stamp.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "date")
    if re.fullmatch(r"-?\d+", raw):
        return Attr(int(raw), "number")
    if re.fullmatch(r"-?\d+\.\d+", raw):
        return Attr(float(raw), "number")
    return Attr(raw, "string")
