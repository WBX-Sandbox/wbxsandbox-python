# Changelog

Changes of the Python SDK from 0.16.0 on (earlier versions are listed in the platform
changelog). The SDK has its own patch numbers; `major.minor` follows the platform release line.

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
