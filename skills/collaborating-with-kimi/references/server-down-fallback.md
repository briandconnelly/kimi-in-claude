# Server-down fallback

Use this fallback only after an MCP transport error shows the stdio server is unavailable.

First ask the user to reconnect or restart the `moonbridge` MCP server, then confirm recovery
with free `kimi_status`. The plugin path is strongly preferred: it supplies workspace-aware diff
gathering, bounded input, best-effort redaction, structured results, the throwaway worktree, and
the orphaned-process cleanup that a bare CLI call has none of.

While the server remains down, a one-off **read-only** consult may use the command below. It
reproduces the one control that actually constrains kimi — an agent profile whose `tools:` list
omits every shell and write tool.

Write the profile first, outside the repository:

```sh
AGENT="$(mktemp -d)/readonly.md"
cat > "$AGENT" <<'EOF'
---
name: moonbridge-readonly
description: Read-only consultant with no shell or write tools.
tools:
  - Read
  - Glob
  - Grep
---
You are a read-only consultant. Answer using only the tools you have.
EOF
```

Then run, from a directory the user approved for disclosure:

```sh
cd "$WORKSPACE" && kimi -p "$PROMPT" \
  --agent-file "$AGENT" \
  --output-format stream-json
```

- **Keep `--agent-file`.** It is the only thing preventing writes. If `kimi` rejects it, stop and
  surface the CLI drift — never drop it to make the command run. Without it the run has Bash and
  Write, with the user's own privileges and no sandbox.
- Do **not** add `--add-dir`: it widens what kimi treats as its workspace.
- `-y`, `--auto`, `--plan`, `--session`, and `--continue` are rejected in prompt mode anyway.
- The prompt is an argv value — kimi ignores stdin. For anything large, write the prompt to a file
  and point at it (`kimi -p "Read /abs/path/prompt.md and follow it exactly."`); argv dies past
  roughly 950k characters with a Node `RangeError`, not a clean error.
- Set `WORKSPACE` to a directory the user approved for disclosure.

What this fallback does **not** buy you:

- **It is not a read boundary.** Read-only bounds writes, not reads: kimi's Read tool accepts
  absolute paths, so it can still read files anywhere on the machine and send them to the
  configured provider. An empty scratch `WORKSPACE` removes ambient repository context but does
  not confine reads.
- Kimi still auto-loads the resolved workspace's `AGENTS.md` and discovers skills from its own
  user/project directories and from the `extra_skill_dirs` entries in its config.toml, which may
  point outside any workspace. Its built-in skills always load. No `WORKSPACE` choice excludes them.
- There is no redaction. Everything you send goes to the provider raw.
- There is no worktree and no orphan cleanup: a command kimi spawns can outlive the run.

If nothing beyond a sanitized prompt may be visible to Kimi, do not use this fallback — wait for
the server.

**Never use a bare CLI call as a delegate substitute.** Without the plugin there is no throwaway
worktree, so kimi edits the user's real tree directly, and no diff is captured for review.
