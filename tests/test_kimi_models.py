"""The model catalog, read from `kimi provider list --json`.

Replaces the Codex-era suite, which tested a `models_cache.json` reader that has no kimi
equivalent: `--model` takes an ALIAS from the user's own config.toml, so the aliases that
will actually work come from the live probe.

The payload shape below is the real one captured from kimi-code 0.35.0 — including the
`providers` block, because the single most important property here is that its `apiKey`
never reaches a result.
"""

from __future__ import annotations

import json

import pytest

from moonbridge import kimi_models
from moonbridge._core.runtime import BINARY_NOT_FOUND, CommandRun

# The real SHAPE of `kimi provider list --json`, with invented values throughout: a real
# provider id, model alias, or base URL names private infrastructure. Only the apiKey is a
# deliberate marker, so the never-leaks assertions have a genuine secret to catch.
REAL_PAYLOAD = {
    "providers": {
        "acme": {
            "baseUrl": "https://example-provider.invalid/v1",
            "type": "openai",
            "apiKey": "sk-SECRET-MUST-NOT-LEAK",
        }
    },
    "models": {
        "acme/model-one": {
            "provider": "acme",
            "model": "vendor/Model-One",
            "maxContextSize": 430000,
            "capabilities": ["thinking", "always_thinking", "tool_use"],
            "displayName": "Model One (Acme)",
            "supportEfforts": ["low", "high", "max"],
            "defaultEffort": "max",
        }
    },
}


@pytest.fixture
def probe(monkeypatch):
    """Drive read_model_catalog from a canned `kimi provider list --json` result."""

    def _set(payload, *, exit_code=0, binary_missing=False, timed_out=False):
        stdout = payload if isinstance(payload, str) else json.dumps(payload)
        run = CommandRun(
            stdout,
            BINARY_NOT_FOUND if binary_missing else "",
            127 if binary_missing else exit_code,
            5,
            timed_out,
        )
        monkeypatch.setattr("moonbridge._core.runtime.run_sync_capture", lambda *a, **k: run)

    return _set


# --------------------------------------------------------------------------- #
# The secret that lives in this payload
# --------------------------------------------------------------------------- #
def test_the_provider_api_key_never_reaches_the_catalog(probe):
    """`kimi provider list --json` returns apiKey in plaintext. The parser is allowlist-
    shaped (it reads named fields off `models` only) precisely so a secret cannot ride
    along, and this asserts the whole serialized result, not just the fields we expect."""
    probe(REAL_PAYLOAD)
    catalog = kimi_models.read_model_catalog()
    blob = json.dumps(catalog.model_dump(mode="json"))
    assert "sk-SECRET-MUST-NOT-LEAK" not in blob
    assert "apiKey" not in blob


def test_the_provider_base_url_never_reaches_the_catalog(probe):
    # A base URL can name private infrastructure; it is not needed to pick a model.
    probe(REAL_PAYLOAD)
    blob = json.dumps(kimi_models.read_model_catalog().model_dump(mode="json"))
    assert "example-provider.invalid" not in blob


def test_the_secret_check_can_actually_fail():
    """Negative control for the two tests above: if the marker were absent from the source
    payload, they would pass no matter what the parser did."""
    assert "sk-SECRET-MUST-NOT-LEAK" in json.dumps(REAL_PAYLOAD)


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def test_reads_aliases_from_the_live_probe(probe):
    probe(REAL_PAYLOAD)
    catalog = kimi_models.read_model_catalog()
    assert catalog.source == "cache"
    assert [m.slug for m in catalog.models] == ["acme/model-one"]


def test_reads_effort_metadata(probe):
    probe(REAL_PAYLOAD)
    model = kimi_models.read_model_catalog().models[0]
    assert model.supported_reasoning_efforts == ["low", "high", "max"]
    assert model.default_reasoning_effort == "max"
    assert model.display_name == "Model One (Acme)"


def test_an_alias_without_effort_metadata_reports_none(probe):
    probe({"models": {"a/b": {"provider": "x"}}})
    model = kimi_models.read_model_catalog().models[0]
    assert model.supported_reasoning_efforts is None
    assert model.default_reasoning_effort is None


def test_junk_effort_tokens_are_dropped(probe):
    probe({"models": {"a/b": {"supportEfforts": ["low", 42, None, "high", "low"]}}})
    model = kimi_models.read_model_catalog().models[0]
    assert model.supported_reasoning_efforts == ["low", "high"]


def test_an_all_junk_effort_list_is_none_not_empty(probe):
    """None means "unknown" and must not be confused with [] ("nothing allowed") — the
    pre-spend guard refuses a run only when the supported set is genuinely known."""
    probe({"models": {"a/b": {"supportEfforts": [1, 2, 3]}}})
    assert kimi_models.read_model_catalog().models[0].supported_reasoning_efforts is None


def test_an_explicitly_empty_effort_list_stays_empty(probe):
    probe({"models": {"a/b": {"supportEfforts": []}}})
    assert kimi_models.read_model_catalog().models[0].supported_reasoning_efforts == []


def test_the_effort_list_is_capped(probe):
    probe({"models": {"a/b": {"supportEfforts": ["low"] * 500}}})
    efforts = kimi_models.read_model_catalog().models[0].supported_reasoning_efforts
    assert efforts == ["low"]


def test_aliases_failing_the_slug_pattern_are_dropped(probe):
    probe({"models": {"good/alias": {}, "bad alias with spaces": {}, "": {}}})
    assert [m.slug for m in kimi_models.read_model_catalog().models] == ["good/alias"]


def test_a_non_dict_entry_does_not_crash_the_parse(probe):
    probe({"models": {"a/b": "not-a-dict"}})
    catalog = kimi_models.read_model_catalog()
    assert [m.slug for m in catalog.models] == ["a/b"]


def test_an_overlong_display_name_is_dropped(probe):
    probe({"models": {"a/b": {"displayName": "x" * 500}}})
    assert kimi_models.read_model_catalog().models[0].display_name is None


# --------------------------------------------------------------------------- #
# Unavailable paths
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "kwargs",
    [
        {"binary_missing": True},
        {"exit_code": 1},
        {"timed_out": True},
    ],
)
def test_a_failed_probe_reports_none_rather_than_guessing(probe, kwargs):
    probe({}, **kwargs)
    catalog = kimi_models.read_model_catalog()
    assert catalog.source == "none"
    assert catalog.unavailable_reason


def test_unparseable_output_reports_none(probe):
    probe("this is not json")
    assert kimi_models.read_model_catalog().source == "none"


def test_a_missing_models_map_reports_none(probe):
    probe({"providers": {"x": {}}})
    assert kimi_models.read_model_catalog().source == "none"


def test_an_oversized_payload_is_refused(probe):
    # Bounded before parse: the probe output is external input.
    probe(json.dumps({"models": {"a/b": {"displayName": "x"}}}) + " " * 2_000_000)
    assert kimi_models.read_model_catalog().source == "none"


# --------------------------------------------------------------------------- #
# supported_efforts_for — the pre-spend guard's source of truth
# --------------------------------------------------------------------------- #
def test_supported_efforts_for_a_known_alias(probe):
    probe(REAL_PAYLOAD)
    assert kimi_models.supported_efforts_for("acme/model-one") == ["low", "high", "max"]


def test_supported_efforts_for_an_unknown_alias_is_none(probe):
    """None means "cannot tell", and callers must NOT reject on it. kimi silently ignores
    an unrecognized effort, so refusing on a guess would block a valid run."""
    probe(REAL_PAYLOAD)
    assert kimi_models.supported_efforts_for("some/other") is None


def test_supported_efforts_for_no_model_is_none(probe):
    probe(REAL_PAYLOAD)
    assert kimi_models.supported_efforts_for(None) is None


def test_supported_efforts_accepts_a_prefetched_catalog(probe):
    """Lets the guard avoid a second subprocess per call."""
    probe(REAL_PAYLOAD)
    catalog = kimi_models.read_model_catalog()
    probe({}, exit_code=1)  # a further probe would now fail
    assert kimi_models.supported_efforts_for("acme/model-one", catalog) == [
        "low",
        "high",
        "max",
    ]
