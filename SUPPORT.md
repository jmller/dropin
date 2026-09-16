# Support

## Supported scope

Dropin v0.1.0 supports macOS 26+ on Apple silicon with Python 3.11+, restic
0.19.1, and rclone 1.75.1. Supported surfaces are the CLI/zipapp, stdio MCP server,
LaunchAgent plist generation, and encrypted restic repositories reached through
rclone.

Earlier macOS versions, Intel Macs, other dependency versions, Homebrew Core, PyPI,
a GUI application, and hosted services are outside the support claim. The
source-available license does not itself include a support entitlement.

## Before requesting help

Preserve the dropped source, state directory, password file and remote repository.
Do not troubleshoot by deleting state, lock files, uncertain restore stages/asides,
or repository data. Run the checks in `docs/operator-guide.md`.

Provide:

- `dropin --version`, `python3 --version`, `restic version`, and `rclone version`;
- macOS version/build and architecture;
- command and exit code;
- human or NDJSON outcome and whether source/state/remote/aside still exist;
- a minimal reproduction using disposable data.

Redact usernames and home paths, repository/remote names, sensitive snapshot IDs,
filenames, tags, comments, content, PIDs, configuration, environment, rclone config,
and every password or token. Never attach private archive data.

Use GitHub issues for ordinary bugs. Use the private instructions in `SECURITY.md`
for vulnerabilities, credentials, or private-metadata exposure.
