# M0 findings — kimi-code 0.35.0 (verified by execution)

| # | Claim | Result |
|---|---|---|
| M0-1 | worktree contains kimi | **FAILED** — cooperative task stayed in; adversarial prompt wrote outside via absolute path. kimi noted "The path is outside my working directory... so I'll run it." No enforcement. |
| M0-2 | prompt/answer file handshake | PASSED — read `.kimi-in-claude/prompt.md`, wrote `answer.md` |
| M0-3 | argv limit | ~950k chars; past it Node dies `RangeError: Maximum call stack size exceeded`, exit 7. Pointer-file mandatory. |
| M0-4 | untrusted worktree loads project `.mcp.json` | NOT loaded — no `mcp__*` tools registered |
| M0-5 | `--skills-dir <empty>` suppresses skills | PARTIAL — replaces user/project dirs only; built-ins (check-kimi-code-docs, update-config, write-goal) always load |
| M0-8 | unknown `-m` alias | `error: failed to run prompt: Model "X" is not configured in config.toml.` exit 1 |
| NEW | `--agent-file` `tools:` allowlist | **PASSED — genuine enforcement.** Agent with `tools: [Read, Glob, Grep]` reported "Available tools: Glob, Grep, Read"; Bash write attempt produced no file. |

| M0-7 | process-group kill reclaims children | **FAILED** — kimi's Bash tool spawns each command in its OWN process group, reparented to init. After `killpg` on kimi's group (3816), `sleep 240` survived as pid 3828/pgid 3828/ppid 1. Orphans persist indefinitely. |

Not yet run: M0-6 (thinking effort).

## M0-7 mitigation
The orphan's command line embeds the worktree path verbatim:
`/bin/bash -c cd '<worktree>' && sleep 240`
Since every run gets a unique `kic-worktree-<rand>` path, a sweep keyed on that string reliably
finds strays. Kill sequence on timeout/cancel: killpg(kimi) -> sweep by worktree path -> SIGTERM,
grace, SIGKILL -> only then `worktree.remove()` (a live writer would otherwise race the removal).

## Other observations
- stdout is clean JSONL; stderr carries raw tool output and thinking. Confirmed.
- `tool_call_id` is NOT unique per run — "Read:0" appeared twice in one session. Parsers must not key on it.
- `system.version` line is emitted on stdout even on a fatal error.
