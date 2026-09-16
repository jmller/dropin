# Test fixtures

Default `mdls/`, `mdimport/`, `xattr/`, and `ownership/` payloads are derived from
real macOS 27.0 build 26A5421a recordings. `provenance.json` maps each payload to
the original redacted JSON under its family's `recordings/` directory:

- `stdout`: exact UTF-8 stdout bytes, no grammar normalization;
- `hex-stdout`: decode native `xattr -p -x` stdout to the original plist bytes;
- `identity`: copy the complete recording JSON, including argv/status/context.

`test_fixture_provenance.py` checks byte-for-byte lineage, inventory, and real
PDF/PPTX metadata replay through capture. Native output remains authoritative;
metadata replay with a fake seam is not a live archive or release test.
Each recordings README describes sample generation, macOS version, redactions and
limitations. Historical failed probes remain as recordings but are not promoted
into successful default fixtures.

`synthetic/{mdls,mdimport,xattr,ownership}/` retains the original deterministic
fixtures unchanged with sibling `.synthetic` markers. Tests for artificial keys,
malformed output, special strings and controlled failure conditions explicitly
use this tree. Pages is only a synthetic regression example; the user-approved
real document sample is PPTX. Synthetic inputs are not evidence of native output.
Never remove a marker merely to claim a fixture is real.

Evidence capture and fixture cleanup are complete. The adapter
validation declaration is complete: `RealMacOS` reports `validated` only for
the recorded command parsing and ownership-control evidence covered here. Live
retrieval/eviction release validation remains separate. The Finder
observation captured no holder; no macOS eviction/restore approval follows from
this fixture migration.
