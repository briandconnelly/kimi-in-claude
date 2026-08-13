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
    assert phrase not in wire_text, (
        f"{phrase!r} appears in the agent-visible surface, but cli_contract.py says it "
        "does not exist. Say what actually runs: `kimi -p` under a generated read-only "
        "agent profile (no shell, no write tools)."
    )


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


def test_status_result_rate_limit_is_structurally_unavailable():
    """The premise of the description test below: no code path can populate this."""
    assert schemas.StatusResult.model_fields["rate_limit"].default_factory().status == "unavailable"


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

# Tokens matching the `kimi_*` shape that are error codes, not tools. Listed explicitly so
# a genuinely missing tool cannot hide behind a broad regex exclusion.
_KNOWN_NON_TOOL_TOKENS = frozenset(
    {
        "kimi_not_found",
        "kimi_auth_required",
        "kimi_auth_indeterminate",
        "kimi_rate_limited",
        "kimi_failed",
    }
)


@pytest.mark.parametrize("code", _TRANSFER_CODES)
def test_transfer_codes_are_not_advertised(wire_text: str, code: str):
    assert code not in wire_text, (
        f"{code} is published in the error-code surface, but no handler emits it and "
        "this server exposes no kimi_transfer tool."
    )


def test_error_code_literal_drops_transfer_codes():
    published = set(schemas.ErrorCode.__args__)
    assert not published & set(_TRANSFER_CODES)


def test_every_repair_hint_names_a_real_callable_surface(wire: dict):
    """The generalized guard. `transfer_unsupported` told agents to "retry kimi_transfer".

    A repair hint that names a tool or resource the server does not expose is worse than
    no hint: the agent spends a turn discovering the surface is absent.
    """
    tool_names = {t["name"] for t in wire["tools"]}
    resource_uris = {r["uri"] for r in wire["resources"]}
    problems = []
    for code, repair in errors._REPAIR_BY_CODE.items():
        blob = repr(repair)
        for named in set(re.findall(r"kimi_[a-z_]+", blob)) - _KNOWN_NON_TOOL_TOKENS:
            if named not in tool_names:
                problems.append(f"{code} -> {named} (not a tool)")
        for uri in set(re.findall(r"kimi://[a-z-]+", blob)) - resource_uris:
            problems.append(f"{code} -> {uri} (not a resource)")
    assert not problems, "repair hints name surfaces that do not exist: " + "; ".join(problems)


def test_repair_hint_guard_catches_a_planted_bad_reference(wire: dict, monkeypatch):
    """Negative control: a clean result must mean the check can fail."""
    tool_names = {t["name"] for t in wire["tools"]}
    assert "kimi_transfer" not in tool_names
    planted = {"bogus_code": ("inspect_and_retry", None, False, "retry kimi_transfer")}
    monkeypatch.setattr(errors, "_REPAIR_BY_CODE", planted)
    with pytest.raises(AssertionError, match="kimi_transfer"):
        test_every_repair_hint_names_a_real_callable_surface(wire)
