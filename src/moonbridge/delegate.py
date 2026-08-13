"""Shared propose-tier orchestration.

`run_delegate` runs a coding task in a throwaway git worktree and returns the normalized
result envelope WITHOUT touching the live tree: the diff is handed back for review, never
applied. The worktree lifecycle itself lives in `runspace`, shared with consult and review;
this module owns only what is specific to the propose tier — the diffstat, secret
redaction, and the byte bound on the returned diff.

Both the synchronous `kimi_delegate` tool and the background `_worker` call this. The
module is import-light (no MCP app) so the worker can use it without constructing the
server.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from moonbridge import config, prompts, runspace
from moonbridge._core import redaction, worktree
from moonbridge.schemas import (
    ContextSummary,
    DelegateResult,
    Meta,
    RawResponse,
    dump_success,
)

if TYPE_CHECKING:
    from collections.abc import Callable


def _diffstat(diff: str) -> ContextSummary:
    """Cheap files/added/removed counts from a unified diff."""
    files = added = removed = 0
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            files += 1
        elif line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return ContextSummary(files_changed=files, lines_added=added, lines_removed=removed)


def _bound_diff(diff: str, meta: Meta, max_bytes: int) -> str:
    """Cap an inline diff at max_bytes, stamping meta.truncated/truncation_hint when
    it overflows. Mirrors the review-diff bound in `_core/gitdiff.py` so a delegate
    run never returns an unbounded diff into the agent's context."""
    encoded = diff.encode("utf-8", "replace")
    if len(encoded) <= max_bytes:
        return diff
    meta.truncated = True
    meta.truncation_hint = (
        f"diff exceeded {max_bytes} bytes and was truncated; narrow the task to a "
        "smaller change, or raise MOONBRIDGE_MAX_DELEGATE_DIFF_BYTES to receive it whole"
    )
    return encoded[:max_bytes].decode("utf-8", "ignore")


# Moved to runspace, which owns the run lifecycle for every tier. Re-exported so the
# existing call sites and tests keep one name for it.
_apply_run_meta = runspace.apply_run_meta


async def run_delegate(
    task: str,
    cwd: str,
    meta: Meta,
    *,
    sandbox: str,
    isolation: str,
    timeout_seconds: int,
    model: str | None,
    reasoning_effort: str | None = None,
    git_timeout: int,
    max_diff_bytes: int | None = None,
    on_worktree_parent: Callable[[str], None] | None = None,
    on_event: Callable[[str], None] | None = None,
) -> dict:
    """Run the propose orchestration and return a DelegateResult|ErrorResult dict.

    `meta` is the pre-built envelope meta (tier=propose). The worktree is always
    cleaned up, even on failure or kimi error. `on_worktree_parent`, if given, is
    called with the temp worktree parent as soon as it exists so a background
    worker can record it for hard-kill cleanup. `max_diff_bytes` caps the inline
    diff (None → the configured default) so a large change cannot flood the agent's
    context; the diffstat still reflects the full diff."""
    outcome = await runspace.run_isolated(
        prompts.build_delegate_prompt(task),
        cwd=cwd,
        meta=meta,
        sandbox=sandbox,
        isolation=isolation,
        timeout_seconds=timeout_seconds,
        model=model,
        reasoning_effort=reasoning_effort,
        git_timeout=git_timeout,
        # The whole point of the propose tier: keep what kimi changed, as a diff the
        # caller reviews. consult/review pass False and discard it with the worktree.
        capture_diff=True,
        on_worktree_parent=on_worktree_parent,
        on_event=on_event,
    )
    if outcome.error is not None:
        return outcome.error
    result = outcome.result
    assert result is not None
    diff = outcome.diff
    # Captured while the worktree existed; it is gone by now, so these aliases are what
    # rewrite worktree-absolute paths out of Kimi's prose below.
    wt_aliases = outcome.aliases

    meta.context_summary = _diffstat(diff)
    # Redact inline secret-looking values from Kimi's free-text (mirroring the diff
    # redaction below, #58: a secret echoed in prose — e.g. quoting a config it read —
    # would otherwise reach the caller unredacted) AND rewrite worktree-absolute paths to
    # repo-relative, since the worktree is gone by now and those paths are dead on arrival
    # (#412). Both passes are in `sanitize_prose` rather than called here in sequence
    # because they interact: neither plain order is safe, and the safe combination is not
    # something a call site should have to remember. See that function for the two attacks.
    # One call covers both fields built from this text (summary and raw_response.text).
    last_message = worktree.sanitize_prose(result.last_message, wt_aliases)
    summary = (last_message or "").strip()
    if not diff.strip():
        if not summary:
            # No diff and no summary is an empty result, not a delegation. Reporting it as
            # a success would hand the caller a DelegateResult carrying nothing at all.
            return runspace.empty_answer_error(meta)
        summary = f"Kimi made no changes. {summary}"
    elif not summary:
        # The diff IS the product here, so a missing summary degrades rather than fails.
        summary = "(kimi returned no summary; review the diff)"
    else:
        # Apply the same secret redaction the review path uses (gitdiff.gather_diff)
        # before the diff leaves this process: drop secret-looking file hunks and
        # replace inline secret values, recording the paths on meta (#57). Redact the
        # full diff first, THEN bound, so the byte cap applies to sanitized text and a
        # secret can't survive inside a truncated fragment. context_summary above is
        # intentionally computed on the pre-redaction diff, mirroring the review path's
        # pre-redaction numstat, so it still reflects the full change.
        diff, meta.redacted_paths = redaction.redact(diff)
        # A cap of None (sync default, or a legacy job spec lacking the key) — or an
        # invalid one from a corrupt spec (non-int, zero, negative) — falls back to
        # the configured, floored default rather than slicing with a bad bound.
        valid_cap = isinstance(max_diff_bytes, int) and max_diff_bytes > 0
        cap = max_diff_bytes if valid_cap else config.max_delegate_diff_bytes()
        diff = _bound_diff(diff, meta, cap)
    return dump_success(
        DelegateResult(
            summary=summary,
            diff=diff or None,
            raw_response=RawResponse(
                text=last_message, session_id=meta.session_id, model=meta.model
            ),
            next_steps=["Review the returned diff; apply it to your tree only if correct."],
            meta=meta,
        )
    )
