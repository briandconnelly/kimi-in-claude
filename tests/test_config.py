"""Config defaults, clamps, env handling, and flag mappings."""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

import pytest

from moonbridge import config


def test_job_store_configures_worktree_cleanup(clean_env):
    import tempfile
    from pathlib import Path

    from moonbridge._core import worktree

    store = config.job_store()
    # The store may clean up only the throwaway-worktree temp area.
    assert store.cleanup_root == Path(tempfile.gettempdir())
    assert store.cleanup_prefix == worktree.WORKTREE_PREFIX


def test_defaults_builtin(clean_env):
    d = config.defaults()
    assert d.tier == "consult"
    assert d.sandbox == "read-only"
    assert d.isolation == "inherit"
    assert d.model is None
    assert d.reasoning_effort is None
    assert d.timeout_seconds == config.DEFAULT_TIMEOUT_SECONDS


def test_default_timeout_seconds_is_300(clean_env):
    # #341: the built-in sync deadline was raised from 180 to 300 to recover the
    # observed mid-tier consult/review runs (246s/267s) that the old cap SIGKILLed.
    # A literal guard so the value cannot drift silently; the clamp bounds are unchanged.
    assert config.DEFAULT_TIMEOUT_SECONDS == 300
    assert (config.MIN_TIMEOUT_SECONDS, config.MAX_TIMEOUT_SECONDS) == (10, 600)


def test_defaults_env_overrides(clean_env):
    clean_env.setenv("MOONBRIDGE_TIER_DEFAULT", "propose")
    clean_env.setenv("MOONBRIDGE_MODEL", "gpt-5.4")
    clean_env.setenv("MOONBRIDGE_REASONING_EFFORT", "high")
    clean_env.setenv("MOONBRIDGE_TIMEOUT_SECONDS", "42")
    d = config.defaults()
    assert d.tier == "propose"
    assert d.sandbox == "workspace-write"  # tier default
    assert d.model == "gpt-5.4"
    assert d.reasoning_effort == "high"
    assert d.timeout_seconds == 42


def test_blank_reasoning_effort_env_is_unset(clean_env):
    # Same convention as MOONBRIDGE_MODEL: a blank env value means "not set".
    clean_env.setenv("MOONBRIDGE_REASONING_EFFORT", "")
    assert config.defaults().reasoning_effort is None


def test_invalid_tier_falls_back(clean_env):
    clean_env.setenv("MOONBRIDGE_TIER_DEFAULT", "nonsense")
    assert config.defaults().tier == "consult"


def test_sandbox_default_override_validated(clean_env):
    clean_env.setenv("MOONBRIDGE_SANDBOX_DEFAULT", "bogus")
    # invalid override -> falls back to the tier's sandbox
    assert config.defaults().sandbox == "read-only"


def test_clamp_timeout():
    assert config.clamp_timeout(1) == config.MIN_TIMEOUT_SECONDS
    assert config.clamp_timeout(99999) == config.MAX_TIMEOUT_SECONDS
    assert config.clamp_timeout(120) == 120


def test_isolation_flags_invalid():
    with pytest.raises(ValueError, match="unsupported isolation"):
        config.isolation_flags("nope")


def test_sandbox_for_tier():
    assert config.sandbox_for_tier("consult") == "read-only"
    assert config.sandbox_for_tier("propose") == "workspace-write"
    assert config.sandbox_for_tier("apply") == "workspace-write"


@pytest.mark.parametrize(
    "value,expected",
    [("${FOO}", True), ("${FOO_BAR2}", True), ("plain", False), ("${}", False), (None, False)],
)
def test_is_env_placeholder(value, expected):
    assert config.is_env_placeholder(value) is expected


def test_placeholder_env_vars(clean_env):
    clean_env.setenv("MOONBRIDGE_MODEL", "${MODEL}")
    clean_env.setenv("MOONBRIDGE_TIMEOUT_SECONDS", "60")
    assert config.placeholder_env_vars() == ["MOONBRIDGE_MODEL"]


@pytest.mark.parametrize(
    "version,expected",
    [("0.35.0", True), ("0.99.0", False), ("garbage", None), (None, None)],
)
def test_version_supported(version, expected, clean_env):
    assert config.version_supported(version) is expected


def test_supported_versions_env_override(clean_env):
    clean_env.setenv("MOONBRIDGE_SUPPORTED_VERSIONS", "0.999")
    assert config.version_supported("kimi-cli 0.999.3") is True
    assert config.version_supported("0.35.0") is False


def test_supported_versions_bad_env_falls_back(clean_env):
    clean_env.setenv("MOONBRIDGE_SUPPORTED_VERSIONS", "garbage")
    assert config.version_supported("0.35.0") is True


def test_state_dir_default(clean_env, monkeypatch):
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    p = config.state_dir()
    assert p.name == "jobs"
    assert "moonbridge" in str(p)


def test_state_dir_override(clean_env, tmp_path):
    clean_env.setenv("MOONBRIDGE_STATE_DIR", str(tmp_path / "jobs"))
    assert config.state_dir() == tmp_path / "jobs"


def test_max_input_bytes_floor(clean_env):
    clean_env.setenv("MOONBRIDGE_MAX_INPUT_BYTES", "5")
    assert config.max_input_bytes() == 1_000


def test_max_delegate_diff_bytes_default(clean_env):
    assert config.max_delegate_diff_bytes() == config.DEFAULT_MAX_DELEGATE_DIFF_BYTES


def test_max_delegate_diff_bytes_override(clean_env):
    clean_env.setenv("MOONBRIDGE_MAX_DELEGATE_DIFF_BYTES", "12345")
    assert config.max_delegate_diff_bytes() == 12345


def test_max_delegate_diff_bytes_invalid_falls_back(clean_env):
    clean_env.setenv("MOONBRIDGE_MAX_DELEGATE_DIFF_BYTES", "notanint")
    assert config.max_delegate_diff_bytes() == config.DEFAULT_MAX_DELEGATE_DIFF_BYTES


def test_max_delegate_diff_bytes_floor(clean_env):
    clean_env.setenv("MOONBRIDGE_MAX_DELEGATE_DIFF_BYTES", "5")
    assert config.max_delegate_diff_bytes() == 1_000


def test_job_defaults(clean_env):
    assert config.job_ttl_seconds() == config.DEFAULT_JOB_TTL_SECONDS
    assert config.job_max_seconds() == config.DEFAULT_JOB_MAX_SECONDS
    assert config.job_max_count() == config.DEFAULT_JOB_MAX_COUNT


def test_job_knobs_clamp_low(clean_env):
    clean_env.setenv("MOONBRIDGE_JOB_TTL", "10")
    clean_env.setenv("MOONBRIDGE_JOB_MAX_SECONDS", "5")
    clean_env.setenv("MOONBRIDGE_JOB_MAX_COUNT", "0")
    assert config.job_ttl_seconds() == 60
    assert config.job_max_seconds() == 60
    assert config.job_max_count() == 1


def test_job_knobs_clamp_high(clean_env):
    clean_env.setenv("MOONBRIDGE_JOB_MAX_SECONDS", "999999")
    clean_env.setenv("MOONBRIDGE_JOB_MAX_COUNT", "999999")
    assert config.job_max_seconds() == 7_200
    assert config.job_max_count() == 1_000


def test_job_knobs_env_override(clean_env):
    clean_env.setenv("MOONBRIDGE_JOB_TTL", "3600")
    clean_env.setenv("MOONBRIDGE_JOB_MAX_SECONDS", "600")
    clean_env.setenv("MOONBRIDGE_JOB_MAX_COUNT", "10")
    assert config.job_ttl_seconds() == 3600
    assert config.job_max_seconds() == 600
    assert config.job_max_count() == 10


def test_max_output_bytes_default(monkeypatch):
    monkeypatch.delenv("MOONBRIDGE_MAX_OUTPUT_BYTES", raising=False)
    assert config.max_output_bytes() == 10 * 1024 * 1024


def test_max_output_bytes_env_override(monkeypatch):
    monkeypatch.setenv("MOONBRIDGE_MAX_OUTPUT_BYTES", "500000")
    assert config.max_output_bytes() == 500_000


def test_max_output_bytes_floor(monkeypatch):
    monkeypatch.setenv("MOONBRIDGE_MAX_OUTPUT_BYTES", "10")
    assert config.max_output_bytes() == 64 * 1024


def test_max_output_bytes_bad_value(monkeypatch):
    monkeypatch.setenv("MOONBRIDGE_MAX_OUTPUT_BYTES", "notanint")
    assert config.max_output_bytes() == 10 * 1024 * 1024


# --- MOONBRIDGE_EXTRA_ARGS passthrough (#231) --------------------------------


def test_extra_args_unset_is_empty_and_valid(clean_env):
    ea = config.extra_args()
    assert ea.tokens == ()
    assert ea.descriptors == ()
    assert ea.option_count == 0
    assert ea.configured is False
    assert ea.valid is True
    assert ea.error is None


def test_extra_args_blank_is_unconfigured(monkeypatch):
    monkeypatch.setenv("MOONBRIDGE_EXTRA_ARGS", "   ")
    ea = config.extra_args()
    assert ea.configured is False
    assert ea.tokens == ()


def test_extra_args_unbalanced_quotes_invalid(monkeypatch):
    monkeypatch.setenv("MOONBRIDGE_EXTRA_ARGS", '-c "model_provider=x')
    ea = config.extra_args()
    assert ea.configured is True
    assert ea.valid is False
    assert ea.tokens == ()
    assert "tokenize" in ea.error


def test_extra_args_rejects_unknown_flag(monkeypatch):
    monkeypatch.setenv("MOONBRIDGE_EXTRA_ARGS", "--json")
    ea = config.extra_args()
    assert ea.valid is False
    assert "unsupported" in ea.error


def test_extra_args_rejects_bare_positional(monkeypatch):
    monkeypatch.setenv("MOONBRIDGE_EXTRA_ARGS", "some-prompt-text")
    ea = config.extra_args()
    assert ea.valid is False


def test_extra_args_rejects_attached_short_form(monkeypatch):
    monkeypatch.setenv("MOONBRIDGE_EXTRA_ARGS", "-cmodel_provider=x")
    ea = config.extra_args()
    assert ea.valid is False


def test_extra_args_denies_approval_policy_key(monkeypatch):
    monkeypatch.setenv("MOONBRIDGE_EXTRA_ARGS", "-c approval_policy=never")
    ea = config.extra_args()
    assert ea.valid is False


def test_extra_args_error_never_echoes_secret_value(monkeypatch):
    # An invalid trailing token must not leak a preceding secret -c value.
    monkeypatch.setenv(
        "MOONBRIDGE_EXTRA_ARGS",
        "-c model_providers.x.api_key=sk-supersecretvalue --bogus",
    )
    ea = config.extra_args()
    assert ea.valid is False
    assert "sk-supersecretvalue" not in (ea.error or "")


def test_extra_args_denies_sandbox_key_with_space_around_dot(monkeypatch):
    monkeypatch.setenv("MOONBRIDGE_EXTRA_ARGS", '-c "sandbox_mode =danger-full-access"')
    ea = config.extra_args()
    assert ea.valid is False


def test_extra_args_denies_shell_environment_policy_key(monkeypatch):
    # host-env exfil vector: exposing the server env to commands kimi runs.
    monkeypatch.setenv("MOONBRIDGE_EXTRA_ARGS", "-c shell_environment_policy.inherit=all")
    ea = config.extra_args()
    assert ea.valid is False


# --- #312: quoted-root spellings may not pass the security root denylist --------------


# --- #287: an operator may not re-enable the remote_plugin connectors -----------------


# --- #310: `model` is reserved for the first-class, meta-reported controls ------------


def test_extra_args_model_denial_is_not_the_remote_plugin_message(monkeypatch):
    # The reserved-key refusal must carry its own explanation, not the
    # remote_plugin security-guarantee text (#287) or the sandbox-roots text.
    monkeypatch.setenv("MOONBRIDGE_EXTRA_ARGS", "-c model=gpt-5-kimi")
    ea = config.extra_args()
    assert ea.valid is False
    assert "remote_plugin" not in ea.error
    assert "sandbox" not in ea.error


# --- #309: `model_reasoning_effort` joins `model` in the reserved set -----------------


# --- Reasoning-effort shape bounds (#309, Kimi re-review) -----------------------------
@pytest.mark.parametrize(
    "value",
    ["", "high", "x" * 128, "an effort with spaces", "Ünïcode-ok", "\xa0"],
)
def test_reasoning_effort_shape_accepts(value):
    assert config.reasoning_effort_shape_error(value) is None


@pytest.mark.parametrize(
    ("value", "fragment"),
    [
        ("x" * 129, "128"),  # over the max length
        ("with\x00nul", "control character"),
        ("with\x07bell", "control character"),
        ("high\n", "control character"),  # trailing newline is NOT admitted here
        ("\x7f", "control character"),  # DEL
        ("high\x80", "control character"),  # C1 lower bound
        ("high\x85", "control character"),  # NEL — a C1 control (category Cc)
        ("high\x9b", "control character"),  # CSI — C1 upper bound
        ("high\ud800", "surrogate"),  # lone high surrogate — hostile to UTF-8/JSON
        ("\udfff", "surrogate"),  # surrogate range upper bound
    ],
)
def test_reasoning_effort_shape_rejects(value, fragment):
    reason = config.reasoning_effort_shape_error(value)
    assert reason is not None
    assert fragment in reason
    # The reason is value-free (safe for an error message).
    assert value not in reason


def test_reasoning_effort_shape_rejects_every_unicode_cc_control():
    # Maintainer-review regression (#313): the documented contract is "no control
    # characters", which is Unicode category Cc — C0, DEL, AND the C1 block
    # (U+0080-U+009F, e.g. NEL/CSI). Both the character-wise predicate and the
    # advertised JSON-Schema pattern must reject every one of them; the first
    # non-control neighbours (space, U+00A0) must pass both.
    cc = [chr(cp) for cp in range(0x100) if unicodedata.category(chr(cp)) == "Cc"]
    assert len(cc) == 65  # C0 (32) + DEL (1) + C1 (32); Cc has no members past U+00FF
    for ch in cc:
        assert config.reasoning_effort_shape_error(ch) == "contains a control character"
        assert re.fullmatch(config.REASONING_EFFORT_VALUE_PATTERN, ch) is None
    for ch in (" ", "\xa0"):
        assert config.reasoning_effort_shape_error(ch) is None
        assert re.fullmatch(config.REASONING_EFFORT_VALUE_PATTERN, ch)


def test_reasoning_effort_shape_rejects_every_surrogate():
    # Maintainer-review regression (#313): surrogate code points (category Cs,
    # U+D800-U+DFFF) are outside Cc but hostile to argv encoding and JSON
    # serialization — an unpaired one raises UnicodeEncodeError before Kimi spawns
    # and breaks envelope serialization. The character-wise predicate rejects the
    # whole range; the neighbours just outside it must pass. (The advertised
    # JSON-Schema pattern deliberately does NOT name the range: under a non-`u`-flag
    # ECMA engine a surrogate class also matches the code UNITS of astral characters,
    # which are legitimate values — see the comment on REASONING_EFFORT_VALUE_PATTERN.)
    for cp in (0xD800, 0xDBFF, 0xDC00, 0xDFFF):
        assert config.reasoning_effort_shape_error(chr(cp)) == "contains a surrogate code point"
    for cp in (0xD7FF, 0xE000, 0x1F600):  # range neighbours + an astral character
        assert config.reasoning_effort_shape_error(chr(cp)) is None


# --------------------------------------------------------------------------- #
# MOONBRIDGE_EXTRA_ARGS — no safe passthrough exists for kimi
# --------------------------------------------------------------------------- #
# The Codex original allowlisted `-c KEY=VALUE`, `-p NAME`, and `--enable/--disable`.
# kimi has none of those, and reuses two of the short flags for other things, so the
# allowlist is empty and any configured value is refused rather than silently ignored.


@pytest.mark.parametrize(
    "value",
    [
        "-p my-profile",  # -p is kimi's PROMPT flag
        "-c key=value",  # -c is kimi's --continue
        "--config model=x",
        "--profile prod",
        "--enable some_feature",
        "--add-dir /etc",  # would defeat worktree isolation
        "--agent-file /tmp/evil.md",  # would replace the read-only agent profile
    ],
)
def test_extra_args_refuses_every_option(monkeypatch, value):
    monkeypatch.setenv("MOONBRIDGE_EXTRA_ARGS", value)
    parsed = config.extra_args()
    assert parsed.configured is True
    assert parsed.valid is False
    assert parsed.tokens == (), "a refused passthrough must inject nothing"


def test_extra_args_refusal_explains_the_flag_collision(monkeypatch):
    """-p and -c mean PROMPT and CONTINUE in kimi. Silently accepting `-p foo` would append
    a second prompt after the plugin's own and override the run's real instructions."""
    monkeypatch.setenv("MOONBRIDGE_EXTRA_ARGS", "-p sneaky")
    parsed = config.extra_args()
    assert parsed.valid is False
    assert "PROMPT" in parsed.error


def test_extra_args_unset_is_valid_and_empty(monkeypatch):
    monkeypatch.delenv("MOONBRIDGE_EXTRA_ARGS", raising=False)
    parsed = config.extra_args()
    assert parsed.configured is False
    assert parsed.valid is True
    assert parsed.tokens == ()


def test_extra_args_blank_is_treated_as_unset(monkeypatch):
    monkeypatch.setenv("MOONBRIDGE_EXTRA_ARGS", "   ")
    assert config.extra_args().configured is False


def test_isolation_levels_are_inherit_and_ignore_skills():
    assert set(config.VALID_ISOLATIONS) == {"inherit", "ignore-skills"}


@pytest.mark.parametrize("isolation", ["inherit", "ignore-skills"])
def test_isolation_flags_are_empty_for_kimi(isolation):
    """kimi has no --ignore-user-config equivalent; isolation is realized via --skills-dir,
    whose value comes from skills_dir_for rather than a standalone flag token."""
    assert config.isolation_flags(isolation) == []


def test_isolation_flags_rejects_an_unknown_level():
    with pytest.raises(ValueError):
        config.isolation_flags("ignore-config")  # a Codex level that no longer exists


def test_skills_dir_is_none_when_inheriting():
    assert config.skills_dir_for("inherit") is None


def test_skills_dir_for_ignore_skills_is_an_existing_empty_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("MOONBRIDGE_STATE_DIR", str(tmp_path))
    path = config.skills_dir_for("ignore-skills")
    assert path is not None
    assert Path(path).is_dir()
    assert not any(Path(path).iterdir())
