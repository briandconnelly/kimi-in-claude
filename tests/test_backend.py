"""KimiBackend: real-adapter validation of the provisional pontifex protocol.

The load-bearing test is the argv differential: the adapter's PreparedRun must
build the SAME command `kimi.run_kimi_exec` builds (normalized for the per-run
handshake paths). If the two diverge, the adapter validates a protocol against
behavior nobody runs.
"""

from __future__ import annotations

import re

import pytest
from pontifex.backend.protocol import AgentBackend, RunOutcome, RunRequest
from pontifex.core.runtime import CommandRun
from pontifex.testing import conformance

from moonbridge import backend as backend_mod
from moonbridge import cli_contract, kimi, kimi_models
from moonbridge.cli_contract import PONTIFEX_CONTRACT
from moonbridge.preflight import FlagSupport

BACKEND = backend_mod.KimiBackend()

_FULL_SUPPORT = FlagSupport(supported=frozenset({"--model", "--skills-dir"}), help_parsed=True)


@pytest.fixture(autouse=True)
def _stable_flag_support(monkeypatch):
    from moonbridge import preflight

    monkeypatch.setattr(preflight, "flag_support", lambda force=False: _FULL_SUPPORT)


def _normalize_handshake(tokens) -> list[str]:
    return [re.sub(r"/[^\s]*moonbridge-handshake-[^/\s]+", "/HANDSHAKE", tok) for tok in tokens]


def test_backend_is_structurally_conformant():
    assert isinstance(BACKEND, AgentBackend)


def test_backend_passes_pontifex_conformance(clean_env):
    assert conformance.check_contract(PONTIFEX_CONTRACT) == []
    assert conformance.check_backend(PONTIFEX_CONTRACT, BACKEND) == []


async def test_schema_instruction_reaches_the_staged_prompt_file(tmp_path, clean_env):
    """The prompt-append wording has ONE source now — `schema_instruction`, owned by
    the adapter since the re-plumb dissolved run_kimi_exec's inline copy. What must
    hold is behavioral: a schema-bearing request stages a prompt file carrying the
    instruction, read inside the prepare context (staging is torn down on exit)."""
    from pathlib import Path

    request = RunRequest(
        kind="consult",
        prompt="the question",
        cwd=str(tmp_path),
        timeout_seconds=60,
        schema={"type": "object"},
    )
    async with BACKEND.prepare(request) as prepared:
        staged = Path(prepared.artifact_paths["prompt"]).read_text(encoding="utf-8")
    assert staged.startswith("the question")
    assert backend_mod.schema_instruction({"type": "object"}) in staged
    assert "# Required output format" in staged


async def test_prepared_argv_matches_production_builder(tmp_path, clean_env):
    """Differential: adapter argv == build_exec_command argv, handshake paths aside."""
    request = RunRequest(
        kind="consult", prompt="why?", cwd=str(tmp_path), timeout_seconds=60, model="m1"
    )
    async with BACKEND.prepare(request) as prepared:
        adapter_argv = _normalize_handshake(prepared.argv)
        assert prepared.stdin_text is None  # kimi ignores stdin
        assert prepared.orphan_marker == str(tmp_path)
        agent_files = [p for p in prepared.artifacts if p.endswith("readonly-agent.md")]
        assert agent_files, "read-only run staged no agent profile"

    expected_cmd, dropped = kimi.build_exec_command(
        cwd=str(tmp_path),
        sandbox=cli_contract.SANDBOX_READ_ONLY,
        isolation="inherit",
        prompt_pointer="/HANDSHAKE-POINTER",
        model="m1",
        agent_file_path="/HANDSHAKE/readonly-agent.md",
        flag_support=_FULL_SUPPORT,
    )
    assert dropped == []
    # Compare shape: same flags in the same order; pointer/agent values are per-run.
    adapter_flags = [t for t in adapter_argv if t.startswith("-")]
    expected_flags = [t for t in expected_cmd if t.startswith("-")]
    assert adapter_flags == expected_flags


async def test_delegate_kind_gets_answer_file_not_agent_file(tmp_path, clean_env):
    request = RunRequest(kind="delegate", prompt="do", cwd=str(tmp_path), timeout_seconds=60)
    async with BACKEND.prepare(request) as prepared:
        assert not any(p.endswith("readonly-agent.md") for p in prepared.artifacts)
        assert any(p.endswith(cli_contract.ANSWER_FILE_NAME) for p in prepared.artifacts)
        i = prepared.argv.index("--prompt")
        assert cli_contract.ANSWER_FILE_NAME in prepared.argv[i + 1]


async def test_handshake_staged_outside_workspace_and_cleaned(tmp_path, clean_env):
    from pathlib import Path

    request = RunRequest(kind="consult", prompt="q", cwd=str(tmp_path), timeout_seconds=60)
    async with BACKEND.prepare(request) as prepared:
        prompt_file = next(p for p in prepared.artifacts if p.endswith("prompt.md"))
        assert not prompt_file.startswith(str(tmp_path))  # outside the workspace
        assert Path(prompt_file).exists()
    assert not Path(prompt_file).exists()  # cleaned on context exit


async def test_effort_rides_env_not_argv(tmp_path, clean_env):
    request = RunRequest(
        kind="consult",
        prompt="q",
        cwd=str(tmp_path),
        timeout_seconds=60,
        reasoning_effort="high",
    )
    async with BACKEND.prepare(request) as prepared:
        assert prepared.env[cli_contract.REASONING_EFFORT_ENV] == "high"
        assert "high" not in " ".join(prepared.argv)


def test_validate_request_rejects_non_kimi_effort_token():
    bad = RunRequest(
        kind="consult",
        prompt="q",
        cwd=".",
        timeout_seconds=10,
        reasoning_effort="not-a-real-effort-level",
    )
    rejected = BACKEND.validate_request(bad)
    assert rejected is not None
    assert rejected.code == "invalid_reasoning_effort"


def test_validate_request_consults_the_catalog(monkeypatch):
    monkeypatch.setattr(kimi_models, "supported_efforts_for", lambda model, catalog=None: ["low"])
    bad = RunRequest(
        kind="consult",
        prompt="q",
        cwd=".",
        timeout_seconds=10,
        model="m1",
        reasoning_effort="high",
    )
    rejected = BACKEND.validate_request(bad)
    assert rejected is not None and rejected.code == "invalid_reasoning_effort"
    ok = RunRequest(
        kind="consult",
        prompt="q",
        cwd=".",
        timeout_seconds=10,
        model="m1",
        reasoning_effort="low",
    )
    assert BACKEND.validate_request(ok) is None


def test_validate_request_fails_open_on_unknown_catalog(monkeypatch):
    """Token-valid efforts pass when the catalog cannot answer — same fail-open
    stance as the server's guard (None must never read as 'nothing allowed')."""
    monkeypatch.setattr(kimi_models, "supported_efforts_for", lambda model, catalog=None: None)
    ok = RunRequest(
        kind="consult",
        prompt="q",
        cwd=".",
        timeout_seconds=10,
        model="mystery",
        reasoning_effort="high",
    )
    assert BACKEND.validate_request(ok) is None


def _failed(stderr: str) -> RunOutcome:
    return RunOutcome(
        run=CommandRun(stdout="", stderr=stderr, exit_code=1, elapsed_ms=5, timed_out=False)
    )


def test_classify_failure_delegates_to_production_classifier():
    request = RunRequest(kind="consult", prompt="q", cwd=".", timeout_seconds=10)
    stderr = 'error: failed to run prompt: Model "zap" is not configured in config.toml.'
    adapter_result = BACKEND.classify_failure(_failed(stderr), request)
    direct = kimi.classify_failure(
        CommandRun(stdout="", stderr=stderr, exit_code=1, elapsed_ms=5, timed_out=False)
    )
    assert adapter_result.code == direct.code == "invalid_model"


def test_finalize_prefers_answer_file_over_stream():
    request = RunRequest(kind="delegate", prompt="q", cwd=".", timeout_seconds=10)
    events = '{"role":"assistant","content":"stream answer"}\n'
    outcome = RunOutcome(
        run=CommandRun(stdout=events, stderr="", exit_code=0, elapsed_ms=5, timed_out=False),
        artifact_texts={"answer": "file answer"},
    )
    assert BACKEND.finalize(outcome, request).answer == "file answer"


def test_finalize_never_invents_usage():
    request = RunRequest(kind="consult", prompt="q", cwd=".", timeout_seconds=10)
    outcome = RunOutcome(
        run=CommandRun(stdout="", stderr="", exit_code=0, elapsed_ms=5, timed_out=False)
    )
    assert BACKEND.finalize(outcome, request).usage is None
