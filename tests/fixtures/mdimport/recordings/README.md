# Real macOS recordings

Captured 2026-09-08 on macOS 27.0 build 26A5421a, arm64.
JSON contains argv, returncode, stdout and stderr. Paths to the disposable
samples/home are redacted as <SAMPLE_ROOT>/<HOME>; generated content only.
The recordings contain generated samples only; paths are redacted and provenance
is kept in the accompanying JSON metadata.
Selected recordings now supply default fixtures via `tests/fixtures/provenance.json`.
Legacy synthetic cases retain markers under `tests/fixtures/synthetic/`.
Fixture promotion does not validate release.

2026-09-09 additions, same macOS build: `Evidence.app-d3.json` and
`plain-folder-d3.json` record `/usr/bin/mdimport -d3 -n` on a new disposable
minimal Info.plist app bundle and empty folder. Both yield attributes but no
text. Temporary/home paths are redacted; no personal content was used.
Historical d2 output is retained. The PPTX sample subsequently replaces the Pages sample
requirement with PPTX; no Pages installation is required.

`sample.pptx.json` (2026-09-09, same macOS build) records native d3 importer
output on a genuine one-slide OOXML presentation generated with python-pptx
1.0.2. Exact generated slide text is preserved. Sample/home paths are redacted;
no personal content was used. This is metadata evidence, not an Office save or
archive round-trip.
