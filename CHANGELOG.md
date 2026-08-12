# Changelog

All notable changes to this project are documented here, following
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.1.0] - 2026-08-12

First release.

### Added

- Claude Code plugin and MCP server that call the Kimi Code (`kimi`) CLI: `kimi_consult` for a
  read-only second opinion, `kimi_review_changes` for a structured review of changes gathered from
  git, and `kimi_delegate` for a coding task that returns a reviewable diff rather than editing the
  working tree. Each has an `_async` job variant, with `kimi_job_*` tools to poll, collect, and
  cancel.
- `runspace.run_isolated`: every tier runs in a throwaway git worktree, with consult and review
  additionally constrained by a generated read-only agent profile (no shell, no write tools).
- Orphan sweep in `_core/runtime.py`: kimi's shell tool spawns each command in its own process
  group, so a process-group kill leaves survivors. Swept by the run's unique worktree path on
  timeout, cancellation, and normal completion.
- Local pre-spend validation of `reasoning_effort` against the model alias's declared efforts,
  because kimi silently ignores an unrecognized effort rather than rejecting it.
- `model` takes an alias from the user's `config.toml`. The catalog is read live from
  `kimi provider list --json`, and the provider `apiKey` in that payload never reaches an envelope.
- `KIMI_IN_CLAUDE_EXTRA_ARGS` refuses every value: kimi exposes no safe passthrough and reuses
  `-p`/`-c` for prompt and continue.
- `.mcp.json` installs the server from the public GitHub remote at a pinned release tag
  (`uvx --from git+…@v0.1.0`), so installing needs no clone. `tests/test_packaging.py` asserts the
  pin tracks the version in `pyproject.toml`.
