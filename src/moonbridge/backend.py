"""KimiBackend: this bridge's adapter on the pontonier AgentBackend protocol.

A faithful thin layer over the proven functions in `kimi.py` — handshake
staging, command construction, extraction, and classification delegate to the
same code they always ran.

WHAT IS WIRED, precisely — the distinction matters before you trust anything
here as a description of production:

- `prepare()` IS the hot path. Since the freeze-window re-plumb,
  `kimi.run_kimi_exec` stages every model-bearing run through it, so it cannot
  drift from production behavior — it is production behavior. That re-plumb also
  DISSOLVED the schema-instruction duplication this module used to pin with a
  parity test: the prompt-append wording now lives only in `schema_instruction`.
- `validate_request`, `finalize`, `classify_failure`, `list_models` and
  `auth_probe` have NO production callers yet. Production reaches the same
  behavior by other routes: `server._reasoning_effort_unsupported_error` runs
  the pre-spend effort guard, and `orchestration`/`runspace` call
  `kimi.classify_failure` directly. These methods are held to the pontonier
  conformance suite, not to differential parity with those routes, so they can
  drift from them. Two known gaps are noted at their definitions.

`PONTONIER_CONTRACT.effort_validation` therefore describes THIS ADAPTER's
`validate_request`, not the server guard that actually runs today; the two agree
on the catalog layer and its fail-open stance, and differ on the floor.

Protocol-fit history: the catalog-relative effort-validation finding landed as
`BackendContract.effort_validation` (pontonier 0.3.0); the named-artifact and
dropped-flags channels this hot path needs landed as
`PreparedRun.artifact_paths`/`.dropped_flags` (0.4.0).
"""

from __future__ import annotations

import contextlib
import json
import shutil
from typing import TYPE_CHECKING

from pontonier.backend.protocol import ClassifiedFailure, ExecResult, PreparedRun, Usage
from pontonier.core import runtime

from moonbridge import cli_contract, config, kimi, kimi_models, normalize
from moonbridge.cli_contract import PONTONIER_CONTRACT

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from pontonier.backend.protocol import RunOutcome, RunRequest

CONTRACT = PONTONIER_CONTRACT


def schema_instruction(output_schema: dict) -> str:
    """The prompt-appended structured-output instruction — the only copy of this
    wording. `kimi.run_kimi_exec` reaches it through `prepare()`."""
    return (
        "\n\n# Required output format\n"
        "Reply with a single JSON object and nothing else — no prose, no code fence. "
        "It must validate against this JSON Schema:\n\n"
        f"{json.dumps(output_schema, indent=2)}\n"
    )


class KimiBackend:
    """The behavior half of the Kimi contract (facts live on PONTONIER_CONTRACT)."""

    def validate_request(self, request: RunRequest) -> ClassifiedFailure | None:
        # Verified on 0.35.0: kimi silently ignores an unrecognized effort (exit 0,
        # default-effort answer), so pre-spend validation is the ONLY protection — and
        # pontonier's conformance check makes it mandatory while
        # `effort_silently_ignored_upstream` is set. Two layers: the vocabulary floor
        # (a value matching no kimi effort level at all is always wrong), then the
        # catalog-relative check, which fails OPEN when the alias is unknown.
        #
        # The floor deliberately reads REASONING_EFFORT_VOCABULARY, not the
        # similarly-named REASONING_EFFORT_TOKEN_PATTERN: that pattern exists to scan
        # kimi's rejection prose and omits `minimal`/`xhigh`, so `fullmatch`ing it here
        # refused two efforts param_contracts advertises to agents.
        effort = request.reasoning_effort
        if effort is None:
            return None
        if effort.strip().lower() not in cli_contract.REASONING_EFFORT_VOCABULARY:
            return ClassifiedFailure(
                code="invalid_reasoning_effort",
                detail="the requested reasoning_effort matches no kimi effort level.",
            )
        supported = kimi_models.supported_efforts_for(request.model)
        if supported and effort not in supported:
            return ClassifiedFailure(
                code="invalid_reasoning_effort",
                detail="the requested reasoning_effort is not one this model declares.",
            )
        return None

    @contextlib.asynccontextmanager
    async def prepare(self, request: RunRequest) -> AsyncIterator[PreparedRun]:
        """Stage exactly what `kimi.run_kimi_exec` stages: the out-of-workspace
        handshake dir (prompt file; generated read-only agent profile or answer
        file), the argv pointer, and the effort/output-format environment."""
        read_only = (
            request.access == cli_contract.SANDBOX_READ_ONLY
            if request.access is not None
            else request.kind != "delegate"
        )
        prompt_text = request.prompt
        if request.schema is not None:
            prompt_text += schema_instruction(request.schema)
        handshake_dir = kimi.create_handshake_dir()
        try:
            paths = kimi.write_handshake(handshake_dir, prompt_text, read_only=read_only)
            cmd, dropped = kimi.build_exec_command(
                cwd=request.cwd,
                sandbox=cli_contract.SANDBOX_READ_ONLY
                if read_only
                else cli_contract.SANDBOX_WORKSPACE_WRITE,
                isolation=request.isolation or config.defaults().isolation,
                prompt_pointer=kimi.build_prompt_pointer(paths, read_only=read_only),
                model=request.model,
                agent_file_path=paths.get("agent"),
                skills_dir=config.skills_dir_for(request.isolation or config.defaults().isolation),
                extra_args=config.extra_args().tokens,
            )
            yield PreparedRun(
                argv=tuple(cmd),
                env=kimi.build_run_env(request.reasoning_effort),
                cwd=request.cwd,
                stdin_text=None,  # kimi ignores stdin
                orphan_marker=request.cwd
                if len(request.cwd) >= runtime.MIN_ORPHAN_MARKER_LENGTH
                else None,
                artifacts=tuple(paths.values()),
                artifact_paths=dict(paths),
                dropped_flags=tuple(dropped),
            )
        finally:
            shutil.rmtree(handshake_dir, ignore_errors=True)

    def finalize(self, outcome: RunOutcome, request: RunRequest) -> ExecResult:
        # Same precedence as kimi._resolve_answer: the answer FILE (propose tier;
        # its text must be read back inside the prepare context — the handshake
        # dir is gone by now) wins over the event stream. A read-only run has no
        # answer file, so the stream is its only channel.
        answer = outcome.artifact_texts.get("answer") or normalize.extract_final_message(
            outcome.run.stdout
        )
        usage, session_id = normalize.parse_event_metadata(outcome.run.stdout)
        structured = normalize.parse_structured(answer) if request.schema is not None else None
        # LOSSY, and unreachable today (nothing in src/ calls finalize — see the module
        # docstring). pontonier's `Usage` has no `cached_input_tokens`, but moonbridge's
        # own `schemas.Usage` does and `normalize.parse_event_metadata` populates it, so
        # the production path reports a field this mapping drops. Wiring `finalize`
        # without first carrying that field across would silently zero it out of every
        # envelope. Widen `pontonier.backend.protocol.Usage` (or thread the moonbridge
        # Usage through) before making this the hot path.
        return ExecResult(
            answer=answer or "",
            structured=structured,
            usage=Usage(
                input_tokens=getattr(usage, "input_tokens", None),
                output_tokens=getattr(usage, "output_tokens", None),
                total_tokens=getattr(usage, "total_tokens", None),
            )
            if usage is not None
            else None,
            session_id=session_id,
        )

    def classify_failure(self, outcome: RunOutcome, request: RunRequest) -> ClassifiedFailure:
        # Unreachable today: production calls `kimi.classify_failure` directly from
        # `runspace._finish` and `orchestration`. Those callers pass two arguments this
        # one cannot, and both must be solved before this becomes the hot path:
        #   - `sanitize=` — runspace hands in `worktree.sanitize_prose`, which relativizes
        #     AND redacts worktree paths in one pass. Without it an isolated run's error
        #     prose can name a torn-down absolute worktree path. The protocol gives no
        #     channel for the alias tuple that sanitizer closes over.
        #   - `last_message` — improves classification; only `events` is available here.
        info = kimi.classify_failure(
            outcome.run,
            events=outcome.run.stdout,
            reasoning_effort=request.reasoning_effort,
        )
        return ClassifiedFailure(
            code=info.code,
            detail=info.message,
            retry_after_ms=info.retry_after_ms,
        )

    def list_models(self) -> tuple[str, ...]:
        return tuple(m.slug for m in kimi_models.read_model_catalog().models)

    def auth_probe(self) -> bool | None:
        configured, _detail = kimi.login_status()
        return configured

    def scrub_env(self, env: dict[str, str], config_mode: str | None) -> dict[str, str]:  # noqa: ARG002 — protocol signature; kimi has no config modes
        return env


# The adapter is stateless; every production path shares this instance.
BACKEND = KimiBackend()
