"""Outcome records, renderers, and the exit-code policy.

Success is quiet, refusals are loud, and the exit code is the machine-readable
summary of the run. Verification failures outrank ordinary item failures because
`get`/`verify` callers must be able to tell "bytes are not trusted" from
"something was refused" without parsing output.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from enum import Enum
import json
import re
from urllib.parse import parse_qsl, unquote, urlsplit

EXIT_OK = 0
EXIT_ITEM_FAILURE = 1
EXIT_USAGE = 2
EXIT_RUN_REFUSED = 3
EXIT_VERIFICATION = 4


class Outcome(str, Enum):
    ARCHIVED = "archived"
    ALREADY_ARCHIVED = "already-archived"
    DEFERRED = "deferred"
    REFUSED = "refused"
    RETAINED = "retained"
    VERIFIED = "verified"
    CORRUPT = "corrupt"
    MISSING = "missing"
    RESTORED = "restored"
    ORPHANED = "orphaned"
    QUEUED = "queued"
    INFO = "info"


#: Nothing was archived and the run must not claim success.
ITEM_FAILURES = frozenset({Outcome.DEFERRED, Outcome.REFUSED, Outcome.RETAINED})
#: Bytes are not trusted.
VERIFICATION_FAILURES = frozenset({Outcome.CORRUPT, Outcome.MISSING})
REDACTED = "[REDACTED]"


def diagnostic_secrets(config) -> tuple[str, ...]:
    """Return concrete configured credentials that outbound diagnostics must hide."""
    found: set[str] = set()
    password_file = getattr(config, "password_file", None)
    if password_file is not None:
        try:
            password = password_file.read_text(errors="replace").strip()
        except OSError:
            password = ""
        if password:
            found.add(password)
    repository = getattr(config, "repo", "")
    if "://" in repository:
        parsed = urlsplit(repository)
        for value in (parsed.username, parsed.password):
            if value:
                found.add(value)
                found.add(unquote(value))
        for _key, value in parse_qsl(parsed.query, keep_blank_values=False):
            if value:
                found.add(value)
    return tuple(sorted(found, key=len, reverse=True))


def redact_diagnostic(value: str | None, secrets=()) -> str | None:
    if value is None:
        return None
    for secret in secrets:
        if not secret:
            continue
        if len(secret) < 8:
            value = re.sub(
                rf"(?<![A-Za-z0-9_]){re.escape(secret)}(?![A-Za-z0-9_])",
                REDACTED, value)
        else:
            value = value.replace(secret, REDACTED)
    return value


def redact_payload(value, secrets=()):
    """Recursively redact strings while preserving a JSON-compatible shape."""
    if isinstance(value, str):
        return redact_diagnostic(value, secrets)
    if isinstance(value, list):
        return [redact_payload(item, secrets) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_payload(item, secrets) for item in value)
    if isinstance(value, dict):
        return {key: redact_payload(item, secrets) for key, item in value.items()}
    return value


@dataclass(frozen=True)
class OutcomeRecord:
    verb: str
    outcome: Outcome
    name: str
    run_id: str
    archive_path: str | None = None
    kind: str | None = None
    state: str | None = None
    snapshot: str | None = None
    sha256: str | None = None
    size: int | None = None
    dedup: bool = False
    reason: str | None = None

    def to_dict(self, secrets=()) -> dict:
        payload = {
            "verb": self.verb,
            "outcome": self.outcome.value,
            "archive_path": self.archive_path,
            "name": self.name,
            "kind": self.kind,
            "state": self.state,
            "snapshot": self.snapshot,
            "sha256": self.sha256,
            "size": self.size,
            "dedup": self.dedup,
            "reason": self.reason,
            "run_id": self.run_id,
        }
        raw = self.name.encode("utf-8", "surrogateescape")
        if raw.decode("utf-8", "replace") != self.name:
            # Non-UTF-8 name: JSON gets an escaped best effort plus the bytes.
            payload["name"] = raw.decode("utf-8", "replace")
            payload["name_b64"] = base64.b64encode(raw).decode("ascii")
        return redact_payload(payload, secrets)

    def to_json(self, secrets=()) -> str:
        return json.dumps(self.to_dict(secrets), ensure_ascii=False)

    def to_human(self, secrets=()) -> str:
        detail = self.reason if self.reason is not None else (self.archive_path or "")
        return redact_diagnostic(
            f"{self.outcome.value}\t{self.name}\t{detail}", secrets)


def exit_code_for(records, run_refusal: str | None = None,
                  run_error: str | None = None) -> int:
    """The documented policy, in one place so verbs cannot drift apart."""
    if run_refusal:
        return EXIT_RUN_REFUSED
    outcomes = {record.outcome for record in records}
    if outcomes & VERIFICATION_FAILURES:
        return EXIT_VERIFICATION
    if run_error or outcomes & ITEM_FAILURES:
        return EXIT_ITEM_FAILURE
    return EXIT_OK


def status_exit_code(*, attention: bool, unreachable: bool,
                     offline: bool = False) -> int:
    """`status` is observational and uses its own documented policy."""
    if unreachable and not offline:
        return EXIT_RUN_REFUSED
    return EXIT_ITEM_FAILURE if attention else EXIT_OK


@dataclass
class Report:
    verb: str
    run_id: str
    records: list[OutcomeRecord] = field(default_factory=list)
    run_refusal: str | None = None
    #: Observation-only failure after item work began; no invented item outcome.
    run_error: str | None = None

    def add(self, record: OutcomeRecord) -> OutcomeRecord:
        self.records.append(record)
        return record

    def item(self, outcome: Outcome, name: str, **fields) -> OutcomeRecord:
        return self.add(OutcomeRecord(verb=self.verb, outcome=outcome, name=name,
                                      run_id=self.run_id, **fields))

    def counts(self) -> dict[Outcome, int]:
        counts: dict[Outcome, int] = {}
        for record in self.records:
            counts[record.outcome] = counts.get(record.outcome, 0) + 1
        return counts

    def exit_code(self) -> int:
        return exit_code_for(self.records, self.run_refusal, self.run_error)

    def render_json(self) -> str:
        return "\n".join(record.to_json() for record in self.records)

    def render_human(self) -> str:
        return "\n".join(record.to_human() for record in self.records)
