# Changelog

All notable changes to this project are documented here, following
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- Initial fork from `codex-in-claude`, retargeted at the Kimi Code (`kimi`) CLI.
- `runspace.run_isolated`: every tier runs in a throwaway git worktree, with consult and review
  additionally constrained by a generated read-only agent profile (no shell, no write tools).
- Orphan sweep in `_core/runtime.py`: kimi's shell tool spawns each command in its own process
  group, so a process-group kill leaves survivors. Swept by the run's unique worktree path on
  timeout, cancellation, and normal completion.
- Local pre-spend validation of `reasoning_effort` against the model alias's declared efforts,
  because kimi silently ignores an unrecognized effort rather than rejecting it.

### Changed

- `.mcp.json` installs the server from the public GitHub remote at a pinned release tag
  (`uvx --from git+…@vX.Y.Z`) instead of launching a hard-coded local checkout with
  `uv run --directory`. Installing no longer requires a clone, and the launch no longer depends on
  a path that exists only on the author's machine. `tests/test_packaging.py` now asserts the pin
  tracks the version in `pyproject.toml`.
- `model` now takes an alias from the user's `config.toml`; the catalog is read live from
  `kimi provider list --json`, and the provider `apiKey` in that payload never reaches an envelope.
- `KIMI_IN_CLAUDE_EXTRA_ARGS` refuses every value: kimi exposes no safe passthrough and reuses
  `-p`/`-c` for prompt and continue.

### Removed

- Session transfer, the app-server client, and live rate-limit reads — kimi exposes no equivalent.

### Fixed

- `redaction.redact` dropped the trailing newline, which made every delegate diff fail
  `git apply`. Inherited from `codex-in-claude`, which likely has the same defect.
