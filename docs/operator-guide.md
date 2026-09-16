# Dropin operator guide

This guide covers v0.1.0 operation on the supported platform: macOS 26+ on Apple
silicon with Python 3.11+, restic 0.19.1, and rclone 1.75.1. Keep the repository
password and rclone credentials outside the drop and state directories.

Examples use the installed command directly. After `dropin setup`, the
configuration is discovered automatically from `$DROPIN_CONFIG` or
`~/.config/dropin/config.toml`; `--config` is only needed for an override.

## Safety and privacy boundaries

Dropin intentionally removes local originals after its configured source, archive,
catalog, and ownership checks pass. Those checks fail closed where possible, but no
software, filesystem, device, network, storage provider, or operator process can make
data loss impossible. Keep an independent known-good copy of irreplaceable data until
you have tested `recover --into` and a restore from the remote repository. Continue
to test recovery periodically; a successful command is not a substitute for a backup
and restore plan. “Verified”, “safe”, and “supported” refer to the checks and scope
documented here, not a warranty; see [`../LICENSE`](../LICENSE).

Only the restic repository is encrypted by this workflow. The drop folder,
configuration, password file, local state/catalog, diagnostics, restore staging, and
restored output are not encrypted by Dropin. The catalog can contain filenames,
paths, metadata, tags, comments, and captured text. Use appropriate macOS account,
disk-encryption, backup, and access controls, and protect rclone credentials
separately.

The MCP server is local stdio software, not a hosted privacy boundary. Depending on
the MCP client, queries and returned names, paths, metadata, and captured text may be
sent to a model provider. Review client approvals and the provider's privacy,
retention, and training settings before connecting private archives. Restic, rclone,
remote storage, MCP clients, and model providers are independent third parties with
their own terms and failure modes.

## Installation paths

On a supported Apple silicon Mac, the shortest path is Homebrew; the formula
installs Dropin, Python, restic, and rclone together:

```sh
brew install jmller/tap/dropin
dropin --version
```

Without Homebrew, download and inspect `scripts/install-release.sh` from the
repository (or a reviewed source archive), then run it. It downloads the release
zipapp and `SHA256SUMS`, verifies the checksum, and atomically installs
`~/.local/bin/dropin`:

```sh
sh scripts/install-release.sh
```

Do not use a `curl | sh` pipeline. The checkout path below is for source
contributors and local evaluation.

## Prerequisite check

From a source checkout, run the standalone shell check:

```sh
scripts/install-prerequisites.sh
```

It checks Python 3.11+, restic 0.19.1+, and rclone 1.75.1+ and returns nonzero if
anything is missing, outdated, malformed, or fails to run. The check is
side-effect-free: it neither installs software nor triggers Apple's Command Line
Tools installer. Install prerequisites separately and rerun it before Dropin.

The installed Dropin zipapp is self-contained and stdlib-only. Do not create a
virtual environment for routine operation. A venv is only an optional isolation
choice for source development or installation through pip.

## First setup

After installation, run the guided command once:

```sh
dropin setup
```

It prompts for the repository and defaults to `~/Drop` and
`~/.local/state/dropin`. Once setup finishes, there is nothing else to configure:
drop a file into `~/Drop` and run `dropin drain`. For automation, pass `--repo`,
`--drop-dir`, and `--state-dir`; with non-interactive input, all three must be
supplied. If the configuration already exists, setup asks for the exact
uppercase response `YES` before replacing it; any other response leaves it
unchanged. `--force` skips that confirmation for automation. Setup retains
`init`'s secure password generation and repository safety rules.

Repository references must use `rclone:REMOTE:/absolute/path` syntax. The path
following the remote must begin with `/`; for example, use
`rclone:local:/Users/me/DropinArchive`, not `rclone:local:DropinArchive`. This
prevents a local rclone backend from selecting a different repository when the
command is launched from another directory or by launchd. Existing cloud
references such as `rclone:onedrive:/dropin-archive` are retained verbatim.

If retrieval reports `no-repo` or repository unavailable, check the configured
remote, path, credentials, and network first. Do not run `restic init` or repair
a repository merely because a retrieval could not find or open it.

## Daily operation

Place settled files or directories in the configured drop directory, then run:

```sh
dropin drain
```

For retrieval, search for the item and restore the selected archive path:

```sh
dropin find --name report
dropin get OCCURRENCE/PATH -o ~/Restored
```

Use `dropin status` or `dropin verify --all` when you want health and
maintenance checks. These commands use the remembered default configuration;
add `--config /path/to/config.toml` only when selecting another configuration.

A successful archive operation captures metadata, uploads encrypted payload and
catalog data, verifies both, confirms that the source is unchanged and unheld, and
only then removes the source. A deferred, retained, refused, corrupt, or missing
result needs attention. `--json` emits NDJSON; do not publish diagnostics without
reviewing and redacting paths and private metadata.

Queries (`find`, `show`, and `ls`) use only the local catalog. They remain available
when the repository is offline. Upload, verify, recover, unlock, and restore require
the configured repository and exact supported tools.

## AI-native recovery through MCP

Dropin includes an optional newline-delimited JSON-RPC MCP server for AI clients.
Connect it through the client once; it inherits the same default configuration
and password-file access as the CLI:

```sh
claude mcp add --transport stdio --scope user dropin -- \
  dropin mcp
```

No daemon, endpoint, or additional setup is required. The assistant can use the
same simple `find` → `show` → `get` workflow for easy control.

The server exposes three tools:

- `find` searches confirmed catalog entries and can filter by name, UTI/kind,
  dates, tags, size, hash, and captured text;
- `show` returns the full captured metadata for one archive path or SHA-256;
- `get` restores one archive path to an explicit existing destination directory.

An assistant can therefore identify a file from a natural-language description,
inspect the candidate, and restore it without being given raw repository access.
`get` still runs Dropin's normal verified restore path. It refuses ambiguous
identifiers and does not overwrite an existing destination unless the client
explicitly passes `force`; review the assistant's proposed path before allowing
that option.

MCP is a recovery and query interface, not an autonomous archival policy: it does
not expose `drain`, deletion, `verify`, or `recover`. If the local catalog is gone,
first rebuild it into a new empty state directory with `recover --into`, validate
queries and a verified restore, update the client configuration, and only then
retire the old state.

## Launchd

`init --launchd` writes but does not load the LaunchAgent. Load it explicitly:

```sh
launchctl bootstrap "gui/$(id -u)" \
  "$HOME/Library/LaunchAgents/dev.dropin.drain.plist"
```

Before changing or removing the agent, unload it and then remove only its plist:

```sh
launchctl bootout "gui/$(id -u)" \
  "$HOME/Library/LaunchAgents/dev.dropin.drain.plist"
rm "$HOME/Library/LaunchAgents/dev.dropin.drain.plist"
```

Do not remove the configuration, state directory, password file, or repository as
part of LaunchAgent removal.

## Locks and offline operation

Dropin permits one writer per state directory. If another writer holds the local
lock, wait for the named process to finish. The lock is an OS lock and is released
when its process exits; do not delete `writer.lock` to bypass it.

A restic repository lock is separate. First ensure no backup, recovery, verification,
or maintenance process still uses the repository. Then inspect status and, only for
a genuinely stale repository lock, run:

```sh
dropin unlock
```

If the remote is unavailable, dropped sources remain local and query commands remain
usable. Restore connectivity and rerun `drain`; do not move or delete state/export
files to force progress.

## Verification, recovery, and diagnostics

Run regular verification and investigate every nonzero exit:

```sh
dropin status
dropin verify --all
```

To prove remote-only recovery, preserve the current state directory, recover into a
new empty directory, and inspect it before changing the active configuration:

```sh
dropin recover --into /absolute/empty/recovery-state
```

Never recover over the only existing state copy. Confirm recovered queries and a
verified restore before retiring old state.

When reporting a problem, include the Dropin version, macOS build/architecture,
restic/rclone versions, command, exit code, and redacted output. Never include the
password file, rclone configuration, credentials, private metadata, or repository
URLs containing secrets. See [`../SUPPORT.md`](../SUPPORT.md) and
[`../SECURITY.md`](../SECURITY.md).

## Password backup and rotation

The restic password is the archive key. Back up the password file separately from
this Mac and test recovery from that backup. Losing every valid key/password makes
recovery impossible.

Do not rotate by merely editing the configured file. Use restic's key-management
procedure for the configured repository, retain the old key and password backup,
then run `verify --all` and a remote-only recovery with the new configuration.
Remove an old restic key only after those checks pass.

## Upgrade and rollback

Before installing an artifact, verify `SHA256SUMS`. Stop/unload launchd, preserve a
backup of the executable, configuration, password, and state, then replace only the
executable. Re-run `--version`, `status`, local queries, and `verify --all` before
resuming scheduled drains.

Rollback likewise replaces only the executable. A version that reports a newer
configuration, SQLite schema, or repository format as incompatible must not be
forced to continue. Reinstall the compatible version; do not edit schema numbers or
repository metadata.

## Uninstall and local purge

A normal uninstall removes only the executable and launch-agent file; it retains the
configuration, password, state, drop directory, and remote repository. Homebrew users
should use `brew uninstall dropin`; standalone users can remove
`~/.local/bin/dropin`.

For a clean reinstall, use the explicit destructive purge. It removes the configured
Dropin configuration, password file, state directory, launch-agent plist, and the
standalone executable when supplied. It does **not** remove the drop directory,
external Python/restic/rclone installations, or the remote repository:

```sh
dropin --config ~/.config/dropin/config.toml uninstall --purge --dry-run
dropin --config ~/.config/dropin/config.toml uninstall --purge --yes
```

`--yes` is required for non-interactive use. Add `--purge-drop` only when the drop
folder contains no originals that must be retained. Pass `--binary` for a standalone
executable path when Dropin cannot infer it. The remote repository and its password
backup should be retained unless you have separately verified exports and recovery.

## Restore interruption

A macOS interruption may retain `.dropin-restore-*` or `.dropin-aside-*`, or may have
published verified output before reporting success. Inspect the destination and
private artifacts manually before retrying. A retry treats existing output as a
collision and does not infer prior success. Do not delete an aside until its prior
content has been identified and retained as required. v0.1.0 does not claim restore
power-loss atomicity.
