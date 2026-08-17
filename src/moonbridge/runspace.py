"""Run kimi inside a disposable workspace — the single place every tier goes through.

Why every tier, not just delegate: `kimi -p` has no sandbox and no approvals (see
cli_contract). Codex could make consult and review read-only with `--sandbox read-only`;
kimi cannot. So this server constrains a run two ways, and both are applied here:

1. **The read-only agent profile** (kimi.read_only_agent_document) removes Bash and Write
   from the model's tool schema. This is the ENFORCING control for consult and review.
2. **A throwaway git worktree** keeps the run's cwd away from the live tree. This is
   defense in depth ONLY — M0 verified that kimi will write outside its working directory
   when asked, so a worktree alone guarantees nothing.

Layering them means a consult has neither the tools to write nor a reason to be near the
real tree, and a delegate's edits land somewhere disposable that we diff deliberately.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pontonier.core import worktree

from moonbridge import config, kimi, normalize
from moonbridge.errors import make_error, serialize_error
from moonbridge.schemas import ErrorDetail, ErrorResult, Meta, Usage

if TYPE_CHECKING:
    from collections.abc import Callable

# Stamped on meta.security_warnings when consult runs outside a git repository. The run is
# still isolated (an empty temp dir), but kimi can read nothing, so an answer that appears
# repo-grounded would be unfounded — the caller has to know that.
NO_REPO_WARNING = (
    "workspace_root is not a git repository, so this ran in an empty temporary directory: "
    "Kimi could not read any repository files, and answered only from the prompt."
)


@dataclass
class RunOutcome:
    """Either a completed kimi run or a ready-to-return error envelope.

    `error` and `result` are mutually exclusive: a non-None `error` means the run never
    produced a usable result and the caller should return it verbatim.
    """

    error: dict | None = None
    result: kimi.KimiRunResult | None = None
    diff: str = ""
    aliases: tuple = field(default_factory=tuple)


def apply_run_meta(meta: Meta, result: kimi.KimiRunResult) -> tuple[Usage | None, str | None]:
    """Stamp a finished run's process metadata onto meta; return (usage, session_id).

    meta.usage is normally None: kimi emits no per-turn token accounting outside goal mode
    (cli_contract). It is left null rather than estimated — a fabricated token count is
    worse than an absent one.
    """
    meta.elapsed_ms = result.run.elapsed_ms
    meta.command_exit_code = result.run.exit_code
    meta.compat_warnings = result.dropped_flags
    kimi.reconcile_dropped_model(result, meta)
    usage, session_id = normalize.parse_event_metadata(result.events)
    meta.usage = usage
    meta.session_id = session_id
    return usage, session_id


def _worktree_error_envelope(exc: Exception, meta: Meta) -> dict:
    if isinstance(exc, worktree.NotAGitRepoError):
        return serialize_error(
            ErrorResult(
                error=make_error(
                    "not_a_git_repo", str(exc), details=ErrorDetail(field="workspace_root")
                ),
                meta=meta,
            )
        )
    return serialize_error(
        ErrorResult(
            error=make_error(
                "worktree_error",
                str(exc)[:300],
                repair_alternative="Ensure the repo has at least one commit and a clean git state.",
            ),
            meta=meta,
        )
    )


async def run_isolated(
    prompt: str,
    *,
    kind: str,
    cwd: str,
    meta: Meta,
    sandbox: str,
    isolation: str,
    timeout_seconds: int,
    model: str | None,
    reasoning_effort: str | None = None,
    git_timeout: int,
    capture_diff: bool,
    output_schema: dict | None = None,
    allow_non_repo: bool = False,
    on_worktree_parent: Callable[[str], None] | None = None,
    on_event: Callable[[str], None] | None = None,
) -> RunOutcome:
    """Run kimi in a disposable workspace and return the outcome.

    `capture_diff` decides what happens to whatever kimi changed: True keeps it (delegate
    returns a reviewable diff), False discards it with the worktree (consult and review
    are answer-only). The worktree is removed either way, including on failure.

    `allow_non_repo` lets consult proceed outside a git repository by running in an empty
    temp directory instead of a worktree — isolation is preserved, but kimi can read
    nothing, so NO_REPO_WARNING is stamped on meta. Never combine it with capture_diff:
    there would be no baseline to diff against.
    """
    if capture_diff and allow_non_repo:  # pragma: no cover - guarded call sites
        raise ValueError("capture_diff needs a git worktree; allow_non_repo cannot apply")

    if allow_non_repo and not worktree.is_git_repo(cwd, timeout=git_timeout):
        meta.security_warnings = [*meta.security_warnings, NO_REPO_WARNING]
        with tempfile.TemporaryDirectory(prefix=config.WORKTREE_CONFIG.prefix) as tmp:
            result = await _invoke(
                prompt,
                kind=kind,
                run_dir=tmp,
                meta=meta,
                sandbox=sandbox,
                isolation=isolation,
                timeout_seconds=timeout_seconds,
                model=model,
                reasoning_effort=reasoning_effort,
                output_schema=output_schema,
                on_event=on_event,
            )
            return _finish(result, meta, diff="", aliases=())

    try:
        wt = worktree.create(
            cwd,
            timeout=git_timeout,
            on_parent=on_worktree_parent,
            config=config.WORKTREE_CONFIG,
        )
    except (worktree.NotAGitRepoError, worktree.NoCommitsError, worktree.WorktreeError) as exc:
        return RunOutcome(error=_worktree_error_envelope(exc, meta))

    # Captured while the worktree still exists: teardown runs before the caller builds any
    # prose, and these aliases are what rewrite worktree-absolute paths back to
    # repo-relative afterwards.
    aliases = worktree.path_aliases(wt.path)
    if wt.baseline_warning:
        meta.security_warnings = [*meta.security_warnings, wt.baseline_warning]

    diff = ""
    try:
        result = await _invoke(
            prompt,
            kind=kind,
            run_dir=wt.path,
            meta=meta,
            sandbox=sandbox,
            isolation=isolation,
            timeout_seconds=timeout_seconds,
            model=model,
            reasoning_effort=reasoning_effort,
            output_schema=output_schema,
            on_event=on_event,
        )
        failed = result.run.exit_code != 0 or result.run.binary_missing or result.run.timed_out
        if not failed and capture_diff:
            diff = worktree.capture_diff(
                wt.path, timeout=git_timeout, config=config.WORKTREE_CONFIG
            )
    except worktree.WorktreeError as exc:
        return RunOutcome(
            error=serialize_error(
                ErrorResult(error=make_error("worktree_error", str(exc)[:300]), meta=meta)
            )
        )
    finally:
        worktree.remove(cwd, wt, timeout=git_timeout)

    return _finish(result, meta, diff=diff, aliases=aliases)


async def _invoke(
    prompt: str,
    *,
    kind: str,
    run_dir: str,
    meta: Meta,
    sandbox: str,
    isolation: str,
    timeout_seconds: int,
    model: str | None,
    reasoning_effort: str | None,
    output_schema: dict | None,
    on_event: Callable[[str], None] | None,
) -> kimi.KimiRunResult:
    result = await kimi.run_kimi_exec(
        prompt,
        kind=kind,
        cwd=run_dir,
        sandbox=sandbox,
        isolation=isolation,
        timeout_seconds=timeout_seconds,
        model=model,
        reasoning_effort=reasoning_effort,
        output_schema=output_schema,
        on_event=on_event,
    )
    apply_run_meta(meta, result)
    return result


def _finish(result: kimi.KimiRunResult, meta: Meta, *, diff: str, aliases: tuple) -> RunOutcome:
    """Convert a failed run into an error envelope; otherwise pass the result through."""
    if result.run.exit_code != 0 or result.run.binary_missing or result.run.timed_out:
        err = kimi.classify_failure(
            result.run,
            last_message=result.last_message,
            events=result.events,
            reasoning_effort=meta.reasoning_effort,
            # kimi ran with cwd inside the workspace, so its raw text can name a directory
            # that is already torn down by the time this envelope reaches the caller.
            # sanitize_prose relativizes AND redacts in one pass; it REPLACES
            # classify_failure's own redaction rather than adding a second one.
            sanitize=lambda t: worktree.sanitize_prose(t, aliases) or "",
        )
        return RunOutcome(error=serialize_error(ErrorResult(error=err, meta=meta)))
    return RunOutcome(result=result, diff=diff, aliases=aliases)


def empty_answer_error(meta: Meta) -> dict:
    """Envelope for a run that succeeded but produced no answer text.

    kimi has no --output-last-message, so for a read-only tier the event stream is the only
    answer channel. If it carries no assistant text there is nothing to report, and
    inventing a success would be worse than failing.
    """
    return serialize_error(
        ErrorResult(
            error=make_error(
                "empty_response",
                "Kimi completed without producing an answer.",
                repair_alternative=(
                    "Retry; if it persists, narrow the question or check kimi_status."
                ),
            ),
            meta=meta,
        )
    )


__all__ = [
    "NO_REPO_WARNING",
    "RunOutcome",
    "apply_run_meta",
    "empty_answer_error",
    "run_isolated",
]
