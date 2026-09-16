# macOS support and limitations

Dropin v0.1.0 is supported on **macOS 26 or newer running on Apple silicon**.
Other macOS versions, Intel Macs, and other operating systems are outside the
release support claim.

## Validated behavior

The supported release scope has been exercised with real macOS metadata and
filesystem behavior, including:

- Spotlight metadata capture for regular files, folders, bundles, and tagged PDFs;
- detection of settled files and files actively held open by the current user;
- encrypted restic/rclone archive, query, verification, and recovery workflows;
- restoration of files, trees, bundles, symlinks, and empty directories;
- collision refusal, force replacement with preservation of the previous target,
  and corruption refusal;
- clean eviction only after capture and remote verification.

These checks use disposable data and do not imply that Dropin can inspect processes
owned by other users.

## Known limitations

- Finder and ownership observations are limited to what the invoking user can see;
  Dropin does not promise per-process forensic attribution.
- An interrupted restore may leave a private staging or aside artifact requiring
  manual inspection. Dropin does not claim power-loss atomicity for macOS restore.
- Hardlink structure is not preserved during restore; linked names retain their
  content as independent files.
- Tags and comments are preserved in the catalog where supported; restored files
  are not promised to receive the original Finder metadata attributes.

A successful Linux test run is not evidence for the macOS support claim. Report
platform-specific problems through the private channel described in `SECURITY.md`.
