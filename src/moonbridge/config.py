"""Config knobs: env defaults, clamps, tier/sandbox/isolation -> kimi flags."""

from __future__ import annotations

import os
import re
import shlex
import tempfile
from dataclasses import dataclass
from pathlib import Path

from pontonier.core import redaction, worktree
from pontonier.core.jobs import JobStore

from moonbridge import cli_contract

# This bridge's pinned worktree knobs. The values predate the pontonier extraction
# and are externally visible (temp-dir names a job runner constrains cleanup to;
# baseline-commit authorship in delegate worktree history; the handshake-dir
# exclusion keeping this server's own plumbing out of reviewed diffs), so they
# must never drift. The exclusion literal is duplicated from
# cli_contract.HANDSHAKE_DIR_NAME; tests assert the two stay in step.
WORKTREE_CONFIG = worktree.WorktreeConfig(
    prefix="moonbridge-worktree-",
    identity_name="moonbridge",
    identity_email="moonbridge@local",
    extra_excludes=(":(exclude,glob)**/.moonbridge/**",),
)

ENV_PREFIX = "MOONBRIDGE_"

MIN_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS = 10, 600
DEFAULT_TIMEOUT_SECONDS = 300
DEFAULT_MAX_INPUT_BYTES = 200_000
# Byte ceiling for a subprocess's captured output (stdout+stderr aggregate), a
# robustness guard against OOM of the long-lived stdio server (#155). Separate
# from MAX_INPUT_BYTES (the diff/input budget) and deliberately generous: the
# JSONL event stream of a long kimi run is large but bounded. Output past the
# cap is dropped (head+tail window kept); the run is NOT killed.
DEFAULT_MAX_OUTPUT_BYTES = 10 * 1024 * 1024
# Byte cap for the diff a delegate run returns inline. Oversized diffs are
# truncated with meta.truncated/meta.truncation_hint so agent token cost stays
# bounded; the diffstat still reflects the full diff.
DEFAULT_MAX_DELEGATE_DIFF_BYTES = 200_000
DEFAULT_GIT_TIMEOUT_SECONDS = 60

# Background-job knobs. TTL: how long a terminal record is kept. MAX_SECONDS: a
# job's wall-clock cap (a poll past it reaps the job). MAX_COUNT: retained records
# per workspace (oldest terminal evicted first).
DEFAULT_JOB_TTL_SECONDS = 86_400
DEFAULT_JOB_MAX_SECONDS = 1_800
DEFAULT_JOB_MAX_COUNT = 50

VALID_TIERS = ("consult", "propose", "apply")
VALID_ISOLATIONS = ("inherit", "ignore-skills")

# Diagnostic logging. Logs go to stderr (and optionally a file); never stdout,
# which is the stdio JSON-RPC channel. WARNING keeps a quiet default while still
# capturing the disconnect/timeout trail a future incident needs (#39).
DEFAULT_LOG_LEVEL = "WARNING"
VALID_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

DEFAULT_TIER = "consult"
DEFAULT_ISOLATION = "inherit"

# Default sandbox for each tier. consult is strictly read-only; propose/apply need
# write access (propose is confined to a temp worktree, apply to the live tree).
TIER_SANDBOX = {
    "consult": cli_contract.SANDBOX_READ_ONLY,
    "propose": cli_contract.SANDBOX_WORKSPACE_WRITE,
    "apply": cli_contract.SANDBOX_WORKSPACE_WRITE,
}


@dataclass
class Defaults:
    tier: str
    sandbox: str
    isolation: str
    model: str | None
    reasoning_effort: str | None
    timeout_seconds: int


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# Shape bounds for a reasoning-effort VALUE (#309), shared by the MCP params (which
# advertise and enforce them at the call boundary) and the pre-spend check on the
# resolved value — the only guard the MOONBRIDGE_REASONING_EFFORT env default
# passes through, since env config never crosses the MCP boundary. The set stays open
# (the backend judges the value); these exclude only argv/serialization-hostile
# shapes: a NUL breaks Popen outright, other control characters have no place in a
# config override, an unpaired surrogate cannot be UTF-8-encoded (it breaks both argv
# encoding and envelope serialization), and an argv-scale string fails as a misleading
# kimi_not_found. "Control character" means Unicode category Cc — C0, DEL, and the
# C1 block (U+0080-U+009F) alike; "surrogate" is category Cs (U+D800-U+DFFF). Real
# efforts are ≤ ~7 chars; 128 is generous headroom.
REASONING_EFFORT_MAX_LENGTH = 128
# ECMA-safe for the advertised JSON-Schema `pattern` (no \Z, which ECMA lacks).
# Deliberately does NOT name the surrogate range: under a non-`u`-flag ECMA engine a
# [\uD800-\uDFFF] class also matches the code UNITS of astral characters — legitimate
# values — so publishing it would make spec-compliant client validators reject them.
# A compliant UTF-8 JSON transport cannot deliver an unpaired surrogate anyway; the
# character-wise check below closes the residual (env defaults, in-process calls,
# lenient parsers).
REASONING_EFFORT_VALUE_PATTERN = r"^[^\x00-\x1F\x7F-\x9F]*$"


def reasoning_effort_shape_error(value: str) -> str | None:
    """Why `value` fails the reasoning-effort shape bounds, or None when it passes.

    Value-free (safe for an error message). Checked character-wise, not via the
    regex, so a trailing newline — which Python's `$` would admit — is caught too."""
    if len(value) > REASONING_EFFORT_MAX_LENGTH:
        return f"exceeds {REASONING_EFFORT_MAX_LENGTH} characters"
    if any(ord(c) < 0x20 or 0x7F <= ord(c) <= 0x9F for c in value):
        return "contains a control character"
    if any(0xD800 <= ord(c) <= 0xDFFF for c in value):
        return "contains a surrogate code point"
    return None


def defaults() -> Defaults:
    tier = os.environ.get(f"{ENV_PREFIX}TIER_DEFAULT", DEFAULT_TIER)
    tier = tier if tier in VALID_TIERS else DEFAULT_TIER
    isolation = os.environ.get(f"{ENV_PREFIX}ISOLATION", DEFAULT_ISOLATION)
    isolation = isolation if isolation in VALID_ISOLATIONS else DEFAULT_ISOLATION
    sandbox = os.environ.get(f"{ENV_PREFIX}SANDBOX_DEFAULT") or TIER_SANDBOX[tier]
    sandbox = sandbox if sandbox in cli_contract.VALID_SANDBOXES else TIER_SANDBOX[tier]
    return Defaults(
        tier=tier,
        sandbox=sandbox,
        isolation=isolation,
        model=os.environ.get(f"{ENV_PREFIX}MODEL") or None,
        reasoning_effort=os.environ.get(f"{ENV_PREFIX}REASONING_EFFORT") or None,
        timeout_seconds=_env_int(f"{ENV_PREFIX}TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS),
    )


# A value the MCP host failed to expand: the literal `${VAR}` form delivered
# verbatim when the host does not perform ${...} substitution. The body must be a
# valid shell variable name so malformed forms are not misreported.
_ENV_PLACEHOLDER_RE = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*\}$")


def is_env_placeholder(value: str | None) -> bool:
    """True when an env value is an unexpanded `${...}` placeholder."""
    return value is not None and bool(_ENV_PLACEHOLDER_RE.match(value.strip()))


def placeholder_env_vars() -> list[str]:
    """Names of tracked `MOONBRIDGE_*` env vars left as unexpanded `${...}`."""
    return sorted(
        name
        for name, value in os.environ.items()
        if name.startswith(ENV_PREFIX) and is_env_placeholder(value)
    )


ENV_PLACEHOLDER_REPAIR = (
    "These env vars are literal ${...}; your MCP host is not expanding env "
    "substitutions. Use an env_vars passthrough list, or set literal values."
)


# --- Opt-in extra `kimi` args passthrough (MOONBRIDGE_EXTRA_ARGS, #231) ----
# An operator-only knob to add extra global `kimi` options to every PAID exec
# invocation (consult/review/delegate) — its motivating use is selecting a
# model_provider/profile when isolation sends --ignore-user-config (which drops the
# user's config.toml, leaving CLI -c overrides the only lever). Deliberately an
# allowlist, not arbitrary argv: a bare positional or unknown flag could clobber the
# envelope-bearing plugin flags (--json/--cd/--sandbox/--output-schema/…) or smuggle a
# prompt, hollowing out the fail-loud CLI contract.
EXTRA_ARGS_ENV = f"{ENV_PREFIX}EXTRA_ARGS"

# --- MOONBRIDGE_EXTRA_ARGS -----------------------------------------------------------
# There is currently NO safe operator passthrough for kimi, so the allowlist is empty and
# any configured value is refused with an explanation.
#
# This is a deliberate narrowing from the Codex original, whose allowlist (`-c KEY=VALUE`,
# `-p NAME`, `--enable/--disable FEATURE`) does not merely fail to apply here — it is
# actively dangerous, because kimi reuses those short flags for different things:
#
#   -p  is kimi's PROMPT flag (a passthrough `-p foo` would append a second prompt after
#       the plugin's own, overriding the run's actual instructions)
#   -c  is kimi's --continue (it would resume a previous session for the working
#       directory instead of running the isolated one this server constructed)
#
# kimi has no `-c KEY=VALUE` config override, no `--profile`, and no feature flags, so
# nothing is lost by refusing. Every other kimi global option is either plugin-owned
# (-p/--output-format/--agent-file/-m/--skills-dir), isolation-defeating (--add-dir), or
# rejected by kimi in prompt mode anyway (-y/--auto/--plan/--session/--continue).
#
# The machinery is kept rather than deleted: it is the natural place to re-open a
# passthrough if kimi grows one, and keeping it means a configured value fails loudly
# instead of being silently ignored.
_EXTRA_CONFIG_FLAGS: tuple[str, ...] = ()
_EXTRA_PROFILE_FLAGS: tuple[str, ...] = ()
_EXTRA_FEATURE_FLAGS: tuple[str, ...] = ()
_PLUGIN_OWNED_FEATURES: frozenset[str] = frozenset()
_DENIED_CONFIG_KEYS: frozenset[str] = frozenset()
_DENIED_CONFIG_KEY_ROOTS: frozenset[str] = frozenset()
_RESERVED_META_CONFIG_KEYS: dict[str, tuple[str, str, str, str]] = {}

NO_PASSTHROUGH_REASON = (
    "kimi has no safe passthrough option: it exposes no config-override, profile, or "
    "feature flags, and its remaining global options are either owned by this plugin, "
    "would defeat worktree isolation, or are rejected in prompt mode. Note that -p and -c "
    "mean PROMPT and CONTINUE in kimi, not profile and config. Unset "
    f"{EXTRA_ARGS_ENV}."
)


@dataclass(frozen=True)
class ExtraArgs:
    """Parsed MOONBRIDGE_EXTRA_ARGS. `tokens` is the validated argv to inject
    (may carry secret `-c` VALUES — never echo it). `descriptors` are sanitized
    identifiers (allowlisted flag names, config KEYS, profile/feature NAMES — never a
    `-c` value) safe to surface in kimi_status / an error envelope and to match against
    a kimi drift stderr. `error` is a value-free 'why invalid' string set only when the
    knob is present but failed to parse/validate; `configured` is True whenever the env
    var is set to a non-blank value."""

    tokens: tuple[str, ...] = ()
    descriptors: tuple[str, ...] = ()
    option_count: int = 0
    configured: bool = False
    error: str | None = None

    @property
    def valid(self) -> bool:
        """True when the knob is unset, or set and parsed/validated cleanly."""
        return self.error is None


def _safe_token(token: str) -> str:
    """A bounded, secret-redacted echo of an offending token for an error message."""
    return (redaction.redact_text(token) or "")[:60]


def _parse_extra_args(raw: str) -> ExtraArgs:
    """Validate a non-blank MOONBRIDGE_EXTRA_ARGS value — which always means rejecting it.

    kimi exposes no option this server can safely pass through (see NO_PASSTHROUGH_REASON),
    so there is nothing to allowlist and this reduces to "any token is refused". It stays a
    parser rather than a bare rejection so the offending token can be named back to the
    operator, and so re-opening a passthrough later is a change here rather than a rewrite.
    """
    try:
        toks = shlex.split(raw)
    except ValueError:
        return ExtraArgs(configured=True, error="could not tokenize (unbalanced quotes?)")
    if not toks:
        return ExtraArgs(configured=True, error="no options found")
    return ExtraArgs(
        configured=True,
        error=f"unsupported argument: {_safe_token(toks[0])} — {NO_PASSTHROUGH_REASON}",
    )


def extra_args() -> ExtraArgs:
    """Resolve MOONBRIDGE_EXTRA_ARGS. Blank/unset → an empty, valid ExtraArgs."""
    raw = os.environ.get(EXTRA_ARGS_ENV)
    if raw is None or not raw.strip():
        return ExtraArgs()
    return _parse_extra_args(raw)


def clamp_timeout(value: int) -> int:
    return max(MIN_TIMEOUT_SECONDS, min(MAX_TIMEOUT_SECONDS, value))


def max_input_bytes() -> int:
    return max(1_000, _env_int(f"{ENV_PREFIX}MAX_INPUT_BYTES", DEFAULT_MAX_INPUT_BYTES))


def max_output_bytes() -> int:
    return max(
        64 * 1024,
        _env_int(f"{ENV_PREFIX}MAX_OUTPUT_BYTES", DEFAULT_MAX_OUTPUT_BYTES),
    )


def max_delegate_diff_bytes() -> int:
    return max(
        1_000,
        _env_int(f"{ENV_PREFIX}MAX_DELEGATE_DIFF_BYTES", DEFAULT_MAX_DELEGATE_DIFF_BYTES),
    )


def git_timeout_seconds() -> int:
    return max(1, _env_int(f"{ENV_PREFIX}GIT_TIMEOUT_SECONDS", DEFAULT_GIT_TIMEOUT_SECONDS))


def job_ttl_seconds() -> int:
    return max(60, _env_int(f"{ENV_PREFIX}JOB_TTL", DEFAULT_JOB_TTL_SECONDS))


def job_max_seconds() -> int:
    return max(60, min(7_200, _env_int(f"{ENV_PREFIX}JOB_MAX_SECONDS", DEFAULT_JOB_MAX_SECONDS)))


def job_max_count() -> int:
    return max(1, min(1_000, _env_int(f"{ENV_PREFIX}JOB_MAX_COUNT", DEFAULT_JOB_MAX_COUNT)))


def job_store() -> JobStore:
    """A JobStore wired to the resolved state dir and job knobs."""
    return JobStore(
        root=state_dir(),
        ttl_seconds=job_ttl_seconds(),
        max_seconds=job_max_seconds(),
        max_count=job_max_count(),
        cleanup_root=Path(tempfile.gettempdir()),
        cleanup_prefix=WORKTREE_CONFIG.prefix,
    )


def sandbox_for_tier(tier: str) -> str:
    """The default sandbox a tier runs under."""
    return TIER_SANDBOX.get(tier, cli_contract.SANDBOX_READ_ONLY)


def isolation_flags(isolation: str) -> list[str]:
    """Kimi flags implementing an isolation level.

    kimi realizes isolation through --skills-dir rather than standalone flags, so this
    returns no tokens; use `skills_dir_for` to build the flag with its value. Kept so the
    tier/isolation plumbing shared with the Codex-shaped orchestration keeps its shape.
    """
    if isolation not in VALID_ISOLATIONS:
        raise ValueError(f"unsupported isolation: {isolation}")
    return []


def skills_dir_for(isolation: str) -> str | None:
    """Directory for --skills-dir under `isolation`, or None to leave discovery alone.

    'ignore-skills' points kimi at a stable empty directory, replacing the auto-discovered
    user/project skill dirs. It does NOT suppress kimi's built-in skills — verified on
    0.35.0 — so this reduces exposure rather than eliminating it.
    """
    if isolation != "ignore-skills":
        return None
    path = Path(state_dir()) / "empty-skills"
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def supported_versions() -> frozenset[tuple[int, int]]:
    """The `kimi` (major, minor) versions this server is built against.

    Overridable via MOONBRIDGE_SUPPORTED_VERSIONS (comma-separated
    "major.minor"). Any parse error falls back to the built-in set."""
    raw = os.environ.get(cli_contract.SUPPORTED_VERSIONS_ENV)
    if not raw:
        return cli_contract.SUPPORTED_VERSIONS
    parsed: set[tuple[int, int]] = set()
    for part in raw.split(","):
        bits = part.strip().split(".")
        if len(bits) < 2:
            continue
        try:
            parsed.add((int(bits[0]), int(bits[1])))
        except ValueError:
            return cli_contract.SUPPORTED_VERSIONS
    return frozenset(parsed) or cli_contract.SUPPORTED_VERSIONS


def parse_version(version: str | None) -> tuple[int, int] | None:
    """Extract (major, minor) from a `kimi --version` string, or None."""
    if not version:
        return None
    match = re.search(r"(\d+)\.(\d+)\.\d+", version)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def version_supported(version: str | None) -> bool | None:
    """Whether the installed kimi (major, minor) is in supported_versions().

    Returns None when unparseable. Advisory only — kimi_status surfaces a mismatch
    as a warning and never blocks calls on it."""
    parsed = parse_version(version)
    if parsed is None:
        return None
    return parsed in supported_versions()


def log_level() -> str:
    """Resolved diagnostic log level (an invalid value falls back to the default)."""
    raw = os.environ.get(f"{ENV_PREFIX}LOG_LEVEL", DEFAULT_LOG_LEVEL).strip().upper()
    return raw if raw in VALID_LOG_LEVELS else DEFAULT_LOG_LEVEL


def log_file() -> str | None:
    """Optional file path mirroring the stderr log, or None (stderr only)."""
    value = os.environ.get(f"{ENV_PREFIX}LOG_FILE")
    return value or None


def state_dir() -> Path:
    """Directory for disk-backed background job records."""
    override = os.environ.get(f"{ENV_PREFIX}STATE_DIR")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_CACHE_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".cache"
    return root / "moonbridge" / "jobs"


def rate_limit_stale_seconds() -> int:
    """Age (seconds) past which a cached snapshot is flagged is_stale. Advisory only —
    the reset-aware interpretation, not this threshold, is the real staleness guard."""
    raw = os.environ.get(f"{ENV_PREFIX}RATE_LIMIT_STALE_SECONDS")
    if raw and raw.isdigit():
        return int(raw)
    return 1800  # 30 minutes


def worktree_base() -> Path | None:
    """Optional override for where temp worktrees are created (default: alongside
    the repo, managed by git). None means let the worktree module choose."""
    override = os.environ.get(f"{ENV_PREFIX}WORKTREE_BASE")
    return Path(override).expanduser() if override else None
