# Contributing

Dropin is source-available under the combined terms in `LICENSE`: Apache License 2.0
subject to Commons Clause License Condition v1.0. It is not open source or
OSI-approved. Contributions are submitted under those terms.

No contributor license agreement or copyright assignment is currently in place. A
submission remains governed by `LICENSE`; do not assume the commercial-licensing note
grants the maintainer additional rights in a third-party contribution. Any broader
grant requires a separate written agreement with its copyright holder.

Discuss broad changes before implementation. Keep commits focused, write a failing
test before behavior or safety changes, and update the public documentation when
behavior changes. Preserve the stdlib-only runtime, one-writer model,
metadata-first catalog, and fail-closed archive/eviction/restore guarantees.

Before submitting:

```sh
make test
```

Changes involving restic/rclone also require `make test-integration` and `make probe`
with the pinned tools. macOS-only claims require evidence from supported real Darwin
hardware. A skipped gate is not a pass.

Use only disposable fixtures. Never commit credentials, password files, local state,
private metadata, raw personal logs, or generated release artifacts. Report
vulnerabilities privately according to `SECURITY.md` rather than in an issue or pull
request.
