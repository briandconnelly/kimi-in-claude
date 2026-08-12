---
description: Preview what a Kimi review would send — scope, diff size, redactions (free)
argument-hint: "[working_tree|branch <base>|commit <sha>]"
---

Call the `kimi_dry_run` MCP tool from the kimi-in-claude server (free — no model
call) to preview what a `kimi_review_changes` call would send.

Scope request: $ARGUMENTS

Map it to `scope`/`base`/`commit` as for /kimi:review, and pass the absolute repo
path as `workspace_root`. Report the context summary (files/lines changed), the
prompt size, whether the diff would be truncated, and any redacted secret paths.
