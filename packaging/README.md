# Release packaging

This directory contains reviewed source inputs for release generation. Generated
artifacts are written to an explicit output directory (normally ignored `dist/`) and
must not be committed.

Planned committed inputs:

- `release-files.txt`: canonical zipapp inclusion manifest.
- `tool-checksums.json`: pinned restic/rclone download metadata.
- `homebrew/Formula/dropin.rb`: staged formula copied to the project-owned tap only
  after its URL and SHA-256 match the published GitHub artifact.

The canonical builder is `scripts/build-release.py`. Do not validate or publish an
ad-hoc `python -m zipapp .` result: building the repository root bypasses artifact
content and secret-exclusion controls.

Before publication, the formula is tested against the exact candidate zipapp through
a temporary local HTTP URL. The public formula may differ only by substituting the
immutable GitHub release URL; its version, checksum, installation logic, and tests
must remain identical. After publication, reinstall from `jmller/homebrew-tap` on a
clean supported account.

Never package credentials, password files, local state, SQLite stores, spool data,
private metadata, Git internals, caches, tests, experiments, specifications, logs, or
previously generated artifacts into the zipapp. The source release is the reviewed
tracked Git tree; repository ignore and security-review gates protect that boundary.
