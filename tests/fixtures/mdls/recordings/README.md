# Real macOS recordings

Captured 2026-09-08 on macOS 27.0 build 26A5421a, arm64.
JSON contains argv, returncode, stdout and stderr. Paths to the disposable
samples/home are redacted as <SAMPLE_ROOT>/<HOME>; generated content only.
The recordings contain generated samples only; paths are redacted and provenance
is kept in the accompanying JSON metadata.
Selected recordings now supply default fixtures via `tests/fixtures/provenance.json`.
Legacy synthetic cases retain markers under `tests/fixtures/synthetic/`.
Fixture promotion does not validate release.

`sample.pptx.json` (2026-09-09, same macOS build) records full native mdls output
and type tree for a genuine one-slide OOXML presentation generated with
python-pptx 1.0.2. This replaces the Pages sample with this user-relevant format;
`.app` still supplies bundle coverage. Sample/home paths are redacted; no
personal content was used.
