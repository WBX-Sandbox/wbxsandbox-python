# Changelog

Changes of the Python SDK from 0.16.0 on (earlier versions are listed in the platform
changelog). The SDK has its own patch numbers; `major.minor` follows the platform release line.

## [0.17.0] – 2026-11-24

### Security

- Redirects are no longer followed: urllib would copy the `X-API-Key` header to the new
  address. A redirect now ends with `SandboxError` naming the target.
- Plain `http://` API addresses (and `ws://` for `wbx connect`) are refused except for
  `localhost`, `127.0.0.1` and `::1`. Use `allow_insecure=True` or `WBXSANDBOX_ALLOW_INSECURE=1`
  for a trusted network.
- `wbx connect --key-file PATH` (or `--key-file -` for standard input) instead of `--key`; a key
  on the command line still works but prints a warning (it is visible in the process list).
- The `verify` extra requires `cryptography>=42.0.4`.

### Changed

- Safe requests (GET, HEAD, DELETE, PUT and POST with an idempotency key) are retried up to
  twice after a connection error, 502/503/504, or 429 with `Retry-After` (`Client(max_retries=…)`).
- `run_job()`, `run_batch()` and `Job.retry()` always send an idempotency key, so a retried
  submission never starts a job twice.
- `Job.download()` verifies the file against the `X-WBX-SHA256` header of the download itself
  (one request instead of two); a missing checksum is an error unless `verify=False`. With
  `dest` the file is streamed to disk in chunks and `None` is returned.
- New `sandbox.files.download(path, dest)` streams a file to disk.
- `sandbox.run(..., on_stdout=…)` raises `SandboxError` when the output stream ends without
  the command's exit (connection lost) instead of returning `exit_code=-1`.
- Version 0.17.0 follows the platform release line.

## [0.16.1] – 2026-11-21

### Changed

- `sandbox.clone()` sends only the arguments you pass (as before – without `timeout` no
  lifetime is sent); the platform (0.16.1) now gives copies the source sandbox's lifetime
  (`timeout`, `on_timeout`, `idle_timeout`, `keep_days`), compute class and network access when
  they are left out. Network options passed to `clone()` can only narrow the source's access.
  Docstrings updated; a test checks that none of these is sent by default.
- Snapshots and clones are available to every account (no longer enabled on request); they can
  still be turned off for an account, then the API answers `403 feature_disabled`.

### Documentation

- README: section on snapshots and clones (examples, what copies inherit, limits, errors).

## [0.16.0] – 2026-11-21

### Added

- Snapshots of a sandbox disk (feature `snapshots`): `sandbox.snapshot(name=None)` returns a
  `Snapshot` (`id`, `name`, `source_sandbox`, `template`, `vcpu`, `mem_mb`, `disk_mb`,
  `size_bytes`, `created_at`); `Snapshot.list()`, `Snapshot.get(id)`, `snapshot.delete()` and
  `snapshot.create_sandbox(**create_args)`.
- `Sandbox.create(from_snapshot="snap_…")` starts a new sandbox from a snapshot. `template` now
  defaults to `None`: without a snapshot the SDK still sends `"base"`, with a snapshot it sends
  no template (the snapshot's template is used).
- Clones (features `snapshots` and `clones`): `sandbox.clone(count=1, …)` creates 1–10 new
  sandboxes with a copy of the disk and returns a `CloneList` (a list of `Sandbox` with
  `snapshot_id` and `errors` for copies that could not be created). It raises `SandboxError`
  only when no copy was created. Takes the create arguments (`timeout`, `env`, `on_timeout`, …),
  `keep_snapshot` and `snapshot_name` (name of the snapshot); `CloneList.snapshot_kept` tells
  whether the snapshot was kept. Each item of `errors` is
  `{"index", "name", "status", "code", "error"}`.
- `Sandbox.create(from_snapshot=…)` waits up to 300 s for the answer (the disk is copied);
  other requests keep the client timeout (120 s by default).
- Error codes `snapshot_limit` (429) and `sandbox_archived` (409) documented on `SandboxError`.
