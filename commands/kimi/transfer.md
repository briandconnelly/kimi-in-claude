---
description: Hand off the current Claude Code session to a resumable Kimi thread
---

Continue this Claude Code conversation inside Kimi by importing its transcript into a
resumable Kimi thread. This is free — no model call, no token spend — but it does
create a thread in `$KIMI_CODE_HOME`.

1. Locate the current session's transcript: the newest `*.jsonl` file under
   `~/.claude/projects/<slug>/`, where `<slug>` is the current working directory with
   `/` replaced by `-` (e.g. `/Users/me/proj` → `-Users-me-proj`). If several are
   plausible or you are unsure which is the active session, ask the user to confirm the
   path rather than guessing.
2. Call the `kimi_transfer` MCP tool from the kimi-in-claude server with
   `transcript_path` set to that absolute path.
3. On success, print the returned `resume_command` (`kimi resume <thread_id>`) so the
   user can open the imported conversation in the Kimi TUI or App.
4. On failure, branch on `error.code` and show `error.repair` — e.g. `transfer_unsupported`
   means the installed Kimi is too old (update it); `transfer_incomplete` means Kimi
   recorded no thread (retry, or fall back to `kimi resume`'s interactive picker).

Note: transferring a still-active session creates a new thread each time you run it —
Kimi only deduplicates a byte-identical transcript — so re-running mid-session is
expected to produce a fresh thread, not the same one.
