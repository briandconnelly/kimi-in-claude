"""The agent-visible surface must not contradict `cli_contract.py`.

This server began as a port of a Codex plugin, and three vestiges of that origin
survived onto the wire: prose describing a `kimi exec` subcommand and a "read-only
sandbox" that `cli_contract.py` says do not exist, a `rate_limit` block advertising a
live quota read that has no upstream channel, and `transfer_*` error codes whose repair
hints name a `kimi_transfer` tool this server does not expose.

Each was a *description* defect, so every gate stayed green: the code was right and the
prose was wrong. These tests read the built manifest — what an agent actually receives —
rather than source text, so a fix that only edits a comment cannot satisfy them.
"""

from __future__ import annotations

import json
import re

import pytest
from pontonier.testing import conformance, surface_honesty

from moonbridge import cli_contract, errors, manifest, schemas


@pytest.fixture(scope="module")
def wire() -> dict:
    """The full agent-visible surface, as `test_manifest.py` builds it."""
    import asyncio

    return asyncio.run(manifest.build_manifest())


@pytest.fixture(scope="module")
def wire_text(wire: dict) -> str:
    return json.dumps(wire, ensure_ascii=False)


# --- m4: vocabulary that contradicts the CLI contract ----------------------------------
# The ban itself lives in cli_contract.py (FORBIDDEN_SURFACE_PHRASES), next to the facts
# that justify it. This module only enforces it against the built wire.


@pytest.mark.parametrize("phrase", cli_contract.FORBIDDEN_SURFACE_PHRASES)
def test_wire_prose_does_not_contradict_the_cli_contract(wire_text: str, phrase: str):
    assert surface_honesty.find_forbidden_phrases(wire_text, (phrase,)) == [], (
        f"{phrase!r} appears in the agent-visible surface, but cli_contract.py says it "
        "does not exist. Say what actually runs: `kimi -p` under a generated read-only "
        "agent profile (no shell, no write tools)."
    )


def test_contract_passes_pontonier_conformance():
    assert conformance.check_contract(cli_contract.PONTONIER_CONTRACT) == []


def test_contract_instance_derives_from_legacy_constants():
    """The declarative PONTONIER_CONTRACT and the constants kimi.py still consumes
    are the same facts in two shapes; pin the derivation so they cannot drift."""
    c = cli_contract.PONTONIER_CONTRACT
    assert c.bin_name == cli_contract.KIMI_BIN
    assert c.always_send_flags == cli_contract.ALWAYS_SEND_FLAGS
    assert set(c.help_gated_flags) == set(cli_contract.HELP_GATED_FLAGS)
    assert c.forbidden_surface_phrases == cli_contract.FORBIDDEN_SURFACE_PHRASES
    assert c.readonly_honesty_statement == cli_contract.READ_ONLY_CONFIDENTIALITY_LIMIT
    assert c.implicit_context_disclosure == cli_contract.SKILLS_DISCOVERY_FACT_FULL
    assert c.usage_event_markers == cli_contract.USAGE_EVENT_MARKERS
    assert c.limits.handshake_dir_name == cli_contract.HANDSHAKE_DIR_NAME
    assert c.limits.answer_file_name == cli_contract.ANSWER_FILE_NAME
    assert c.limits.max_argv_prompt_chars == cli_contract.MAX_ARGV_PROMPT_CHARS
    assert c.effort_silently_ignored_upstream  # the verified 0.35.0 silent-ignore fact


def test_signature_regexes_match_what_the_predicates_match():
    """The contract's regex tables classify the same evidence the predicates do —
    the shared classifier must not weaken classification when the adapter
    migration lands."""
    import re

    samples = {
        "auth": "Error: 401 unauthorized — run `kimi login`",
        "contract_drift": "error: unknown option '--sandbox'",
        "invalid_model": 'Model "zap" is not configured in config.toml.',
        "rate_limited": "429 Too Many Requests",
    }
    sigs = cli_contract.PONTONIER_CONTRACT.failure_signatures
    assert cli_contract.is_auth_failure(samples["auth"])
    assert any(re.search(p, samples["auth"]) for p in sigs.auth)
    assert cli_contract.is_contract_drift(samples["contract_drift"])
    assert any(re.search(p, samples["contract_drift"]) for p in sigs.contract_drift)
    assert cli_contract.is_invalid_model(samples["invalid_model"])
    assert any(re.search(p, samples["invalid_model"]) for p in sigs.invalid_model)
    assert cli_contract.is_rate_limited(samples["rate_limited"])
    assert any(re.search(p, samples["rate_limited"]) for p in sigs.rate_limited)


def test_forbidden_phrases_are_still_contradicted_by_the_contract():
    """Guard the guard: each phrase is banned BECAUSE of a fact that can change.

    If kimi ever grows an `exec` subcommand or a real sandbox, this fails first and tells
    the author to retire the ban rather than leave a stale prohibition in place.
    """
    assert cli_contract.EXEC_SUBCOMMAND == (), "kimi grew an exec subcommand; revisit the ban"
    # "read-only sandbox" is banned because read-only is a tool allowlist, not confinement.
    assert "absolute paths" in cli_contract.READ_ONLY_CONFIDENTIALITY_LIMIT
    assert "Bash" not in cli_contract.READ_ONLY_AGENT_TOOLS


# --- M1: rate_limit advertises a channel that does not exist ---------------------------


# The states the schema still accepts. The description below promises exactly one of them
# is reachable, so the guard has to constrain the TOOL, not the schema default: an earlier
# version of this test asserted only `StatusResult`'s default_factory, which stays green
# while `kimi_status()` constructs its own `RateLimit(status="available")` (Codex review).
_UNREACHABLE_STATES = tuple(s for s in schemas.RateLimitStatus.__args__ if s != "unavailable")


@pytest.mark.parametrize(
    ("version", "login"),
    [
        ("0.35.0", (True, "Kimi reports 1 configured provider(s).")),  # ready
        ("0.35.0", (False, "no provider configured")),  # unauthenticated
        ("0.35.0", (None, None)),  # auth indeterminate — the probe did not answer
        (None, (None, None)),  # binary missing
    ],
    ids=["ready", "unauthenticated", "auth_indeterminate", "kimi_not_found"],
)
def test_kimi_status_returns_unavailable_in_every_readiness_state(
    monkeypatch, clean_env, version, login
):
    """The description's promise, checked against the tool's actual output.

    `unavailable` has to hold on every path, not just the happy one — a readiness branch
    that populated a real quota state would resurrect the spend-planning contradiction the
    description now rules out.
    """
    from moonbridge import server

    monkeypatch.setattr(server.kimi, "kimi_version", lambda: version)
    monkeypatch.setattr(server.kimi, "login_status", lambda: login)
    got = server.kimi_status()["rate_limit"]["status"]
    assert got == "unavailable", (
        f"kimi_status returned {got!r}; the description promises unavailable"
    )
    assert got not in _UNREACHABLE_STATES


def test_rate_limit_schema_default_is_unavailable():
    """Secondary schema check, kept as a floor under the behavioral test above."""
    assert schemas.StatusResult.model_fields["rate_limit"].default_factory().status == "unavailable"
    # Guard the guard: if the Literal ever loses its other states, the parametrization above
    # degenerates to a tautology and this says so.
    assert _UNREACHABLE_STATES, "RateLimitStatus has only one state; the behavioral test is vacuous"


def test_kimi_status_does_not_promise_a_live_quota_read(wire: dict):
    """kimi_status must not teach spend planning around states it cannot emit."""
    desc = next(t for t in wire["tools"] if t["name"] == "kimi_status")["description"]
    for claim in ("fetched LIVE", "spend_control_reached", "prefer to defer"):
        assert claim not in desc, (
            f"kimi_status advertises {claim!r}, but its rate_limit is always "
            "'unavailable' (kimi exposes no quota-read channel). Describe the one "
            "state the tool can actually return."
        )


def test_capability_summary_and_kimi_status_agree_on_quota(wire: dict):
    """The two surfaces contradicted each other; neither may drift back."""
    desc = next(t for t in wire["tools"] if t["name"] == "kimi_status")["description"]
    assert "unavailable" in desc, "kimi_status must state the status it always returns"
    assert "unavailable" in wire["initialize"]["instructions"]


# --- m1: transfer_* codes reference a tool this server does not expose -----------------

_TRANSFER_CODES = ("transfer_unsupported", "transfer_failed", "transfer_incomplete")


# `kimi_*` tokens that are error codes, not tools. DERIVED from the published Literal, not
# hand-listed: the hand-list carried a phantom (`kimi_failed`, never an ErrorCode) that would
# have exempted a genuinely missing tool, and it could not track codes added or removed later.
def _error_code_tokens() -> frozenset[str]:
    return frozenset(c for c in schemas.ErrorCode.__args__ if c.startswith("kimi_"))


# Verbs that make a token a CALL, not a mention. An error code named here is being used as a
# callable surface, so the code-name exemption above must not apply (Codex review: prose like
# "retry kimi_rate_limited" slipped through a blanket token exemption).
_CALL_PHRASE = re.compile(r"\b(?:call|run|rerun|retry|invoke|use)\s+`?(kimi_[a-z_]+)`?", re.I)


@pytest.mark.parametrize("code", _TRANSFER_CODES)
def test_transfer_codes_are_not_advertised(wire_text: str, code: str):
    assert code not in wire_text, (
        f"{code} is published in the error-code surface, but no handler emits it and "
        "this server exposes no kimi_transfer tool."
    )


def test_error_code_literal_drops_transfer_codes():
    published = set(schemas.ErrorCode.__args__)
    assert not published & set(_TRANSFER_CODES)


def _repair_hint_problems(wire: dict, table: dict) -> list[str]:
    """Every callable surface a repair hint names must exist. Three checks, not one.

    1. `repair.tool` is the machine-readable field an agent actually dispatches on, so it is
       validated strictly — no exemptions.
    2. A token inside a call phrase ("retry X") is a callable reference even when it spells
       an error code, so the code-name exemption does not reach it.
    3. Remaining bare mentions may be error codes.
    """
    tool_names = {t["name"] for t in wire["tools"]}
    resource_uris = {r["uri"] for r in wire["resources"]}
    codes = _error_code_tokens()
    problems = []
    for code, repair in table.items():
        blob = repr(repair)
        # (1) the structured field
        machine_tool = repair[1] if isinstance(repair, tuple) and len(repair) > 1 else None
        if machine_tool is not None and machine_tool not in tool_names:
            problems.append(f"{code} -> repair.tool={machine_tool} (not a tool)")
        # (2) call phrases bind tighter than the code-name exemption
        called = {m.lower() for m in _CALL_PHRASE.findall(blob)}
        for named in called - tool_names:
            problems.append(f"{code} -> '{named}' used as a callable (not a tool)")
        # (3) bare mentions: a tool, or a published error code
        for named in set(re.findall(r"kimi_[a-z_]+", blob)) - called - codes - tool_names:
            problems.append(f"{code} -> {named} (neither a tool nor an error code)")
        for uri in set(re.findall(r"kimi://[a-z-]+", blob)) - resource_uris:
            problems.append(f"{code} -> {uri} (not a resource)")
    return problems


def test_every_repair_hint_names_a_real_callable_surface(wire: dict):
    """The generalized guard. `transfer_unsupported` told agents to "retry kimi_transfer"."""
    problems = _repair_hint_problems(wire, errors._REPAIR_BY_CODE)
    assert not problems, "repair hints name surfaces that do not exist: " + "; ".join(problems)


@pytest.mark.parametrize(
    ("planted", "expect"),
    [
        # The original defect: a tool that does not exist, outside any exemption.
        (("inspect_and_retry", None, False, "retry kimi_transfer"), "kimi_transfer"),
        # The hole Codex found: an ERROR CODE used as a callable. A blanket token exemption
        # passed this; the call-phrase rule catches it.
        (("inspect_and_retry", None, False, "retry kimi_rate_limited"), "kimi_rate_limited"),
        (("inspect_and_retry", None, False, "call kimi_not_found first"), "kimi_not_found"),
        # A bad structured repair.tool, which no prose rule would see at all.
        (("inspect_and_retry", "kimi_nonexistent", False, "do a thing"), "kimi_nonexistent"),
        # A resource that does not exist.
        (("list_resources", None, False, "read kimi://nope"), "kimi://nope"),
    ],
    ids=[
        "missing-tool",
        "code-as-callable",
        "code-as-callable-2",
        "bad-repair-tool",
        "bad-resource",
    ],
)
def test_repair_hint_guard_catches_planted_defects(wire: dict, planted, expect):
    """Negative controls: a clean result must mean each of the three checks can fail."""
    problems = _repair_hint_problems(wire, {"planted_code": planted})
    assert any(expect in p for p in problems), f"guard missed a planted {expect!r}; got {problems}"
