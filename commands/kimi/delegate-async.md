---
description: Delegate a coding task to Kimi in the background; get a job_id to poll
argument-hint: "<task description>"
---

Delegate a long-running coding task to Kimi Code in the background using the
`kimi_delegate_async` MCP tool from the kimi-in-claude server.

Task: $ARGUMENTS

Pass the absolute repository path as `workspace_root`. The tool returns a `job_id`
immediately; the run continues detached (it works in an isolated git worktree and
NEVER touches the working tree).

To track and collect it:
1. Poll `kimi_job_status` with the `job_id`. Honor `poll_after_ms` between polls —
   do not poll in a tight loop. The job is bounded by its wall-clock deadline.
2. When `result_available` is true, call `kimi_job_result` to read the envelope
   (same shape as `kimi_delegate`, with a `diff`).
3. Review the `diff` for correctness yourself. Apply it to the working tree (using
   your own edit tools) only if it is correct — tell the user, or ask first for a
   significant change. Do not apply a diff you have not reviewed.
4. Optionally call `kimi_job_consume_result` instead of `kimi_job_result` to read
   and delete the stored record, or `kimi_job_cancel` to stop a running job.
