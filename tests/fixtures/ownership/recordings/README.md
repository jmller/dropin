# Real macOS recordings

Captured 2026-09-08 on macOS 27.0 build 26A5421a, arm64.
JSON contains argv, returncode, stdout and stderr. Paths to the disposable
samples/home are redacted as <SAMPLE_ROOT>/<HOME>; generated content only.
The recordings contain generated samples only; paths are redacted and provenance
is kept in the accompanying JSON metadata.
Selected recordings now supply default fixtures via `tests/fixtures/provenance.json`.
Legacy synthetic cases retain markers under `tests/fixtures/synthetic/`.
Fixture promotion does not validate release.

2026-09-09 addition, same macOS build: `finder-copy.json` records Finder 27.0
AppleScript duplication of a generated 128-MiB/4096-file tree, with bounded
concurrent file/directory lsof observations and a post-return observation.
All lsof samples are empty (exit 1); no open-holder positive was captured.
This is not proof of active-write detection or a release-gate pass. Only disposable
samples were used; root/home paths are redacted. The observation is retained only as bounded fixture provenance, not as a release
or active-write safety claim.
