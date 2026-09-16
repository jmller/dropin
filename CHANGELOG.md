# Changelog

## [Unreleased]

## [0.1.0] - 2026-09-16

Initial public release.

### Added

- Folderless archiving into an encrypted restic repository reached through rclone.
- Spotlight-shaped metadata capture, local search, and remote-only catalog recovery.
- Verified file, tree, and bundle restore with collision refusal and force-aside
  preservation.
- Human and NDJSON CLI output, stdio MCP tools, and bounded launchd drains.
- Deterministic pip-free zipapp, SHA-256 checksums, and a project-owned Homebrew tap
  formula.
- Explicit `uninstall` and confirmation-gated local `--purge` for clean reinstalls;
  remote repositories and drop folders remain protected by default.

### Fixed

- Classify streamed restic retrieval failures using documented repository, lock,
  and password exit categories instead of treating every nonzero dump exit as
  archived-file corruption, while preserving verified content-integrity failures;
  diagnostics retain the exit status and stderr tail.
- Require absolute rclone repository paths so local backends are independent of
  the invoking working directory while preserving cloud remote path semantics.
- Accept reserved MCP `_meta` call metadata so current Codex clients can invoke
  `find`, `show`, and `get` instead of receiving an invalid-parameters error.
- Expand `~` in setup and init paths before creating directories, instead of
  creating a literal `~` directory relative to the current working directory.
- Include `NOTICE` in source, wheel, and zipapp distributions and publish the
  combined source-available terms through PEP 639 package metadata.

### Documentation

- Clarify data-loss, local-catalog, third-party-service, MCP privacy, contribution,
  and separate-commercial-licensing boundaries.

### Compatibility

- Supported only on macOS 26+ on Apple silicon with Python 3.11+.
- Validated with restic 0.19.1 and rclone 1.75.1. Other tool versions, earlier
  macOS releases, and Intel Macs are untested.
- Unknown configuration fields, newer SQLite schemas, and newer/unsupported restic
  repository formats fail closed. v0.1.0 is the first public schema baseline.
- Upgrade and rollback replace only the executable. Configuration, password, state,
  queued sources, and repository must be retained.

### Known limitations

- An interrupted macOS disk restore may retain a private stage/aside or verified
  output before the command reports success. Manual inspection is required; no
  restore power-loss atomicity is claimed.
- Open-holder visibility is limited to the invoking user, does not cover mmap-only
  access, and does not provide complete Finder PID attribution.
- Finder tags, comments, and captured metadata are recoverable in the catalog;
  v0.1.0 does not promise filesystem xattr reapplication during restore.
