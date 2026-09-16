# Security policy

## Supported versions

Only the latest 0.1.x release is supported, on macOS 26+ Apple silicon within the
matrix in `SUPPORT.md`.

## Private reporting

Report suspected vulnerabilities, credential exposure, or private-metadata exposure
to **mail@johann3s.de**. Do not open a public GitHub issue for confidential reports.

Include the Dropin version or commit, macOS build and architecture, impact, and a
minimal reproduction using disposable data. Redact diagnostics. Never send password
files, passwords, tokens, rclone configuration, personal metadata, archive contents,
or repository URLs containing credentials.

Reports are acknowledged, triaged, and coordinated for disclosure on a best-effort
basis; no response-time SLA is promised.

## Archive keys

The restic password is the archive key. If it may be compromised, follow restic's
key-management procedure and verify remote-only recovery with a new key. Merely
editing the configured password file neither rotates repository keys nor removes a
compromised key.
