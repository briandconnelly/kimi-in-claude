# kimi-in-claude

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Call **Kimi Code** from **Claude Code** — an independent second opinion, structured code review,
and delegated coding tasks — through an MCP server that drives the `kimi` CLI.

**Contents:** [Why](#why) · [What this does and does not protect](#what-this-does-and-does-not-protect) ·
[Quick start](#quick-start) · [Requirements](#requirements) · [Tools](#tools) ·
[Configuration](#configuration) · [Local development](#local-development)

## Why

A second model from a different vendor is a cheap, high-value check. `kimi-in-claude` lets a Claude
Code session hand Kimi a question, a diff to review, or a task to implement, and get back a
structured result you stay in control of.

| Tier | How the run is constrained | Where edits go | Use for |
|------|---------------------------|----------------|---------|
| `consult` | read-only agent profile (no shell, no write tools) in a throwaway worktree | nowhere — text only | questions, second opinions |
| `review` | same as consult | nowhere — structured findings | reviewing your git changes |
| `delegate` | full tool set in a throwaway worktree | isolated worktree → a **reviewable diff, never auto-applied** | delegating a coding task |

## What this does and does not protect

Read this before pointing it at anything sensitive. Every statement below was verified by running
`kimi-code 0.35.0`, not inferred from its documentation.

**The `kimi` CLI has no sandbox and no approval prompts.** Prompt mode (`kimi -p`) forces
autonomous mode and runs shell commands and file writes with your own user's privileges. Unlike
Codex, there is no `--sandbox` flag to hand it. So this server constrains runs itself:

- **consult and review get an agent profile whose `tools:` list omits every shell and write
  tool.** This is the real control, and it works: an agent declaring `Read, Glob, Grep` reports
  exactly those three, and a shell write attempt produces nothing.
- **every tier runs in a throwaway git worktree.** This is defense in depth, *not* a boundary —
  asked to write outside its working directory, Kimi will do it. Treat the worktree as keeping
  honest runs tidy, not as containment.

Three limits that follow, stated plainly because they are easy to assume away:

1. **Read-only prevents modification, not disclosure.** Kimi's Read tool accepts absolute paths, so
   a prompt-injected repository can make a consult read files elsewhere on your machine and send
   them to your provider. Do not point any tier at a workspace whose contents you would not hand
   to that provider.
2. **Delegate is not network-isolated.** A delegated task can push, fetch, install dependencies,
   and call out. The returned diff shows what changed *in the worktree* — not everything the run
   did.
3. **Kimi loads context you did not mention.** It auto-loads the workspace's `AGENTS.md` and
   discovers skills from its own user/project directories and from the `extra_skill_dirs` entries
   in its `config.toml`, which may point anywhere on disk. Its built-in skills always load. The
   `isolation` setting reduces this but cannot eliminate it.

Secret redaction covers gathered diffs and Kimi's returned output. It does **not** cover what you
type, or files Kimi reads for itself.

## Quick start

Make sure the `kimi` CLI is installed and a provider is configured:

```sh
kimi --version
kimi provider list --json   # at least one provider and one [models."<alias>"] entry
```

Install the plugin. In Claude Code:

```
/plugin marketplace add briandconnelly/kimi-in-claude
/plugin install kimi-in-claude@kimi-in-claude
```

No clone is needed. The plugin ships [`.mcp.json`](.mcp.json), which installs the server from this
repo at a pinned release tag (`uvx --from git+…@vX.Y.Z`).

Then run `/kimi:status` in Claude Code. It is free — no model call — and reports whether the CLI is
found, in the tested version range, and backed by a configured provider.

For a first useful run:

- `/kimi:consult is this approach sound?` for a read-only second opinion.
- `/kimi:review` to review your current git changes.
- `/kimi:delegate add focused tests for this behavior` to get a proposed diff.

## Requirements

- `kimi` (Kimi Code) **0.35.x** — the version this release is verified against
- Python ≥ 3.11, `uv`, and `git`
- macOS or Linux

## Tools

| Tool | Cost | Notes |
|---|---|---|
| `kimi_status` | free | readiness, version, provider configuration, resolved defaults |
| `kimi_capabilities` | free | full inventory, schemas, per-tool error codes |
| `kimi_models` | free | model **aliases** from your `config.toml`, with each alias's declared efforts |
| `kimi_consult` / `_async` | paid | read-only Q&A |
| `kimi_review_changes` / `_async` | paid | structured review of `working_tree`, `branch`, or `commit` |
| `kimi_delegate` / `_async` | paid | returns a reviewable diff, never applied |
| `kimi_dry_run`, `kimi_delegate_dry_run` | free | preview scope, diff size, redactions before spending |
| `kimi_job_{status,result,consume_result,cancel,list}` | free | background job lifecycle |

Two contract details worth knowing:

- **`model` takes an alias**, not a provider model id — whatever you defined as
  `[models."<alias>"]` in `config.toml`. An unknown alias is rejected as `invalid_model`.
- **`reasoning_effort` is validated locally.** Kimi silently *ignores* an effort it does not
  recognize rather than rejecting it, so this server refuses one the alias does not declare — a run
  that quietly used the default while reporting your requested effort would be worse than an error.

## Configuration

Environment variables, all prefixed `KIMI_IN_CLAUDE_`:

| Variable | Default | Meaning |
|---|---|---|
| `TIMEOUT_SECONDS` | 300 | per-call wall clock, clamped 10–600 |
| `MODEL` | unset | default model alias |
| `REASONING_EFFORT` | unset | default effort |
| `ISOLATION` | `inherit` | `inherit` or `ignore-skills` |
| `MAX_INPUT_BYTES` | 200000 | bound on gathered context |
| `MAX_DELEGATE_DIFF_BYTES` | 200000 | bound on a returned diff |
| `JOB_TTL` / `JOB_MAX_SECONDS` / `JOB_MAX_COUNT` | 86400 / 1800 / 50 | background job limits |
| `STATE_DIR` | `~/.cache/kimi-in-claude/jobs` | job records |
| `LOG_LEVEL` / `LOG_FILE` | `WARNING` / unset | logging |
| `SUPPORTED_VERSIONS` | `0.35` | tested `kimi` minors |
| `EXTRA_ARGS` | unset | **no safe passthrough exists** — any value is refused, see below |

`KIMI_IN_CLAUDE_EXTRA_ARGS` accepts nothing today, deliberately. kimi exposes no config-override,
profile, or feature flags, and reuses two short flags for other purposes: `-p` is **prompt** and
`-c` is **continue**. Passing them through would override the run's real instructions or resume an
unrelated session, so the allowlist is empty and a configured value fails loudly rather than being
silently ignored.

## Local development

```sh
uv sync
uv run pytest                                     # unit + contract suite, 95% coverage floor
uv run pytest -m integration --no-cov             # live tests against the real kimi CLI
uv run ruff check . && uv run ruff format --check . && uv run ty check
```

To install from a checkout, register it as a marketplace: `/plugin marketplace add <path to this
checkout>`, then install as above. Note that this still runs the **released** server: `.mcp.json`
is pinned to a tag, so it is not affected by your edits. To run the working tree, override the
server in the consuming project's own `.mcp.json`:

```json
{
  "mcpServers": {
    "kimi-in-claude": {
      "command": "uv",
      "args": ["run", "--directory", "<path to this checkout>", "kimi-in-claude-mcp"]
    }
  }
}
```

`src/kimi_in_claude/_core/` holds CLI-agnostic machinery and must never import its parent package.
Everything this server assumes about the `kimi` CLI lives in `cli_contract.py`; the captures it was
verified against are in `docs/kimi-help/0.35.0/`.
