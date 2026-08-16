"""KimiBackend: this bridge's adapter on the pontifex AgentBackend protocol.

A faithful thin layer over the proven functions in `kimi.py` — handshake
staging, command construction, extraction, and classification delegate to the
same code `run_kimi_exec` runs, so the adapter cannot drift from production
behavior. Its job today is real-adapter validation of the PROVISIONAL protocol
(pontifex freezes only after all three bridges' adapters fit); re-plumbing
runspace/orchestration through it lands with the freeze.

Protocol-fit findings for pontifex 0.3.0, discovered here:

* Catalog-relative effort validation fails OPEN when the alias's supported set
  is unknowable (`supported_efforts_for` returns None — deliberately never read
  as "nothing allowed"). The conformance invariant "reject a bogus effort
  pre-spend" is therefore stronger than the server's guard; this adapter adds
  kimi's universal effort-token floor (low|medium|high|max) so a value matching
  NO kimi vocabulary is refused even without catalog knowledge. The protocol
  should let a contract declare catalog-relative validation.
* The schema-append instruction text lives inline in `run_kimi_exec`, not in a
  named function — the prompt_append strategy needs a shareable seam before the
  adapter can own structured output without duplicating the wording (duplicated
  here for now, pinned by a parity test).
"""

from __future__ import annotations

import contextlib
import json
import shutil
from typing import TYPE_CHECKING

from pontifex.backend.protocol import ClassifiedFailure, ExecResult, PreparedRun, Usage
from pontifex.core import runtime

from moonbridge import cli_contract, config, kimi, kimi_models, normalize
from moonbridge.cli_contract import PONTIFEX_CONTRACT

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from pontifex.backend.protocol import RunOutcome, RunRequest

CONTRACT = PONTIFEX_CONTRACT


def schema_instruction(output_schema: dict) -> str:
    """The prompt-appended structured-output instruction, byte-identical to the
    inline text in `kimi.run_kimi_exec` (a parity test pins the two)."""
    return (
        "\n\n# Required output format\n"
        "Reply with a single JSON object and nothing else — no prose, no code fence. "
        "It must validate against this JSON Schema:\n\n"
        f"{json.dumps(output_schema, indent=2)}\n"
    )


class KimiBackend:
    """The behavior half of the Kimi contract (facts live on PONTIFEX_CONTRACT)."""

    def validate_request(self, request: RunRequest) -> ClassifiedFailure | None:
        # Verified on 0.35.0: kimi silently ignores an unrecognized effort (exit 0,
        # default-effort answer), so this pre-spend check is the ONLY protection.
        # Two layers: the universal token floor (a value matching no kimi effort
        # vocabulary is always wrong), then the catalog-relative check when the
        # alias's supported set is known.
        effort = request.reasoning_effort
        if effort is None:
            return None
        if not cli_contract.REASONING_EFFORT_TOKEN_PATTERN.fullmatch(effort):
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
            cmd, _dropped = kimi.build_exec_command(
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
