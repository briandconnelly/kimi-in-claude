"""Single source of truth for the external `kimi` CLI contract.

Every assumption this server makes about the `kimi` (Kimi Code) CLI — its flags, the
prompt-mode invocation, the stream-json event surface, the supported major versions, and
the stderr phrasings that mean the contract drifted — lives here so an upstream breaking
change is centralized, greppable, and testable. Revising it takes the lockstep procedure in
docs/UPGRADING-KIMI.md, not an edit to this file alone. See COMPATIBILITY.md for the
assumption -> upstream-source map.

Verified against `kimi-code 0.35.0` on 2026-08-12 by running the binary; the captures live
in docs/kimi-help/0.35.0/.

--- How this differs from the Codex contract, and why it matters -------------------------

`kimi -p` is NOT `codex exec`. Three differences drive the whole design:

1. **There is no sandbox and there are no approvals.** Prompt mode forces auto permission
   mode internally and refuses to combine with -y/--auto/--plan. It runs Bash and writes
   files with zero gating. Codex's `--sandbox read-only` has no equivalent.

   Verified: a worktree does NOT contain kimi. Asked to write to an absolute path outside
   its working directory, it did so, remarking that the path was outside its working
   directory but it would run the command anyway. **A worktree changes kimi's cwd, not its
   reach.** Treat it as defense in depth, never as the guarantee.

   The real control is READ_ONLY_AGENT_TOOLS below, delivered via --agent-file. Verified:
   an agent declaring `tools: [Read, Glob, Grep]` reported exactly those three tools and a
   Bash write attempt produced no file. The tools are absent from the model's schema.

2. **The prompt is argv-only.** stdin is ignored (`echo x | kimi -p` errors on a missing
   argument). argv is bounded near 950_000 characters, and past that kimi dies with a Node
   `RangeError: Maximum call stack size exceeded` (exit 7) rather than a clean error — so
   large context MUST travel via a file, not argv. See PROMPT_FILE_NAME.

3. **There is no --output-last-message.** The final answer is recovered from the
   stream-json stream (ROLE_ASSISTANT lines) or, where the tier has a Write tool, from an
   answer file the prompt asks kimi to produce. See ANSWER_FILE_NAME.
"""

from __future__ import annotations

import re

KIMI_BIN = "kimi"

# --- Core non-interactive invocation ---------------------------------------------------
# kimi has no `exec` subcommand; headless runs ride the top-level `-p/--prompt` flag
# ("prompt mode"). EXEC_SUBCOMMAND stays as an empty tuple so callers can keep building
# `[KIMI_BIN, *EXEC_SUBCOMMAND, ...]` uniformly.
EXEC_SUBCOMMAND: tuple[str, ...] = ()
PROMPT_FLAG = "-p"

# Machine-readable event stream. `text` is the default and prints only the assistant text;
# `stream-json` is the only structured option. Guarantee-bearing: without it there is no
# event surface to parse, so a rejection must fail loudly rather than silently degrade.
OUTPUT_FORMAT_FLAG = "--output-format"
OUTPUT_FORMAT_JSON = "stream-json"

# Model selection takes a config.toml ALIAS, not a raw provider model id.
MODEL_FLAG = "-m"

# Per-run agent profile. This is how consult/review become genuinely read-only — see
# READ_ONLY_AGENT_TOOLS. Cannot be combined with --session/--continue.
AGENT_FILE_FLAG = "--agent-file"

# Replaces auto-discovered user/project skill directories. NOTE (verified): this does NOT
# suppress kimi's BUILT-IN skills, which always load. See SKILLS_ISOLATION_NOTE.
SKILLS_DIR_FLAG = "--skills-dir"

# Extra workspace roots. The plugin never sends this: it would punch a hole straight
# through worktree isolation. Named here so a caller-supplied value can be rejected by name.
ADD_DIR_FLAG = "--add-dir"

# Rejected in prompt mode ("Cannot combine --prompt with --yolo."). Listed so the
# extra-args allowlist can refuse them with a precise reason instead of a generic failure.
PROMPT_MODE_INCOMPATIBLE_FLAGS = (
    "-y",
    "--yolo",
    "--auto",
    "--plan",
    "-S",
    "--session",
    "-c",
    "--continue",
)

# Probes (free; no model call).
VERSION_ARGS = ("--version",)
HELP_ARGS = ("--help",)
EXEC_HELP_ARGS = HELP_ARGS  # prompt mode is top-level, so the root help is the gate source
PROVIDER_LIST_ARGS = ("provider", "list", "--json")
DOCTOR_CONFIG_ARGS = ("doctor", "config")
HELP_CACHE_TTL_SECONDS = 300

# --- The read-only guarantee -----------------------------------------------------------
# The tool allowlist that makes consult and review read-only. GUARANTEE-BEARING: if a kimi
# release ignores or renames the agent-file `tools:` key, consult/review silently regain
# Bash and Write, so a drift here must fail loudly as cli_contract_changed.
#
# Bash is deliberately absent: it can write, so a "read-only" agent holding Bash is not
# read-only. Verified on 0.35.0 that omitting it removes it from the model's tool set.
READ_ONLY_AGENT_TOOLS = ("Read", "Glob", "Grep")
READ_ONLY_AGENT_NAME = "kimi-in-claude-readonly"

# --- The file handshake ----------------------------------------------------------------
# Written inside the worktree, under a single dot-directory kept out of any captured diff.
HANDSHAKE_DIR_NAME = ".kimi-in-claude"
PROMPT_FILE_NAME = "prompt.md"
# Only the propose tier can produce this: a read-only agent has no Write tool.
ANSWER_FILE_NAME = "answer.md"
# Observed ceiling ~950_000 chars before the Node RangeError; held well under it because
# the failure past the edge is a crash, not a clean error.
MAX_ARGV_PROMPT_CHARS = 8_000

# --- Reasoning effort ------------------------------------------------------------------
# kimi has no reasoning-effort FLAG. Effort rides an environment variable read in prompt
# mode, and the accepted values are per-model (`support_efforts` in config.toml).
# Unlike Codex's TOML-encoded `-c` pair, no value encoding is needed.
REASONING_EFFORT_ENV = "KIMI_MODEL_THINKING_EFFORT"
MODEL_REASONING_EFFORT_CONFIG_KEY = "thinking.effort"  # config.toml path, for status prose
MODEL_OUTPUT_FORMAT_ENV = "KIMI_MODEL_OUTPUT_FORMAT"
KIMI_HOME_ENV = "KIMI_CODE_HOME"

# --- Flag classes ----------------------------------------------------------------------
# ALWAYS_SEND: guarantee-bearing. Never help-gated; a rejection is cli_contract_changed.
ALWAYS_SEND_FLAGS = (PROMPT_FLAG, OUTPUT_FORMAT_FLAG, AGENT_FILE_FLAG)
# HELP_GATED: depth-only. Dropped (with its value) when `kimi --help` does not advertise
# it; the drop is surfaced in meta.compat_warnings. Value is True when the flag takes one.
HELP_GATED_FLAGS: dict[str, bool] = {
    MODEL_FLAG: True,
    SKILLS_DIR_FLAG: True,
}

# --- Sandbox vocabulary (posture labels, NOT kimi flags) -------------------------------
# kimi accepts no sandbox flag. These names survive because the tier model and the result
# envelope still describe a posture, and dropping them would silently retype `meta.sandbox`
# for every client. They now denote how the run is CONSTRAINED BY THIS SERVER:
#   read-only       -> read-only agent profile (READ_ONLY_AGENT_TOOLS) inside a worktree
#   workspace-write -> full tool set inside a throwaway worktree; diff returned, not applied
#   danger-full     -> never emitted; retained so the literal stays a closed set
SANDBOX_READ_ONLY = "read-only"
SANDBOX_WORKSPACE_WRITE = "workspace-write"
SANDBOX_DANGER_FULL = "danger-full-access"
VALID_SANDBOXES = (SANDBOX_READ_ONLY, SANDBOX_WORKSPACE_WRITE, SANDBOX_DANGER_FULL)

# Codex's `--disable remote_plugin` has no kimi equivalent; kept as inert names so callers
# referencing them keep type-checking while the disclosure prose carries the real story.
DISABLE_FEATURE_FLAG = ""
REMOTE_PLUGIN_FEATURE = ""

# --- Versions --------------------------------------------------------------------------
SUPPORTED_VERSIONS = frozenset({(0, 35)})
SUPPORTED_VERSIONS_ENV = "KIMI_IN_CLAUDE_SUPPORTED_VERSIONS"

# --- Models ----------------------------------------------------------------------------
# `-m` takes an alias defined in config.toml, so there is no meaningful bundled slug list;
# the catalog is read live from `kimi provider list --json`. Kept empty (not removed) so
# kimi_models' fallback path stays type-correct and returns "no catalog" rather than lying.
KNOWN_MODEL_SLUGS: tuple[str, ...] = ()
MODEL_SLUG_PATTERN = re.compile(r"^[A-Za-z0-9._/-]{1,128}$")
MODELS_CACHE_FILENAME = "config.toml"
MODELS_CACHE_MAX_BYTES = 1_000_000
MODELS_CACHE_MAX_ENTRIES = 256
SUPPORTED_EFFORTS_MAX_ENTRIES = 16

# --- stream-json event surface ---------------------------------------------------------
# Verified line shapes on stdout (stderr carries raw tool output, thinking, and warnings —
# never feed stderr to the JSONL parser):
#   {"role":"meta","type":"system.version","version":"0.35.0"}        <- always first
#   {"role":"assistant","content":"...","tool_calls":[...]}
#   {"role":"tool","tool_call_id":"...","content":"..."}
#   {"role":"meta","type":"turn.step.retrying",...}
#   {"role":"meta","type":"session.resume_hint","session_id":"...",...}  <- always last
#   {"type":"goal.summary",...}                                       <- goal mode only
#
# Two parsing traps, both verified:
#   * `tool_call_id` is NOT unique within a run ("Read:0" appeared twice in one session),
#     so it must never be used as a correlation key.
#   * `goal.summary` carries no "role" field, unlike every other line.
ROLE_KEY = "role"
TYPE_KEY = "type"
ROLE_META = "meta"
ROLE_ASSISTANT = "assistant"
ROLE_TOOL = "tool"
CONTENT_KEY = "content"
TOOL_CALLS_KEY = "tool_calls"
EVENT_SYSTEM_VERSION = "system.version"
EVENT_RESUME_HINT = "session.resume_hint"
EVENT_TURN_RETRYING = "turn.step.retrying"
EVENT_GOAL_SUMMARY = "goal.summary"
SESSION_ID_KEY = "session_id"
VERSION_KEY = "version"

# kimi emits no per-turn token accounting outside goal mode, so `meta.usage` stays null on
# ordinary runs. Kept for the goal.summary path, which does carry a token count.
USAGE_EVENT_MARKERS = ("tokensUsed", "turnsUsed")

# Goal-mode process exit codes (prompts beginning `/goal`). Not used by v1; recorded so the
# meaning of a non-zero exit is not mis-classified if goal mode is adopted later.
GOAL_EXIT_CODES = {0: "complete", 3: "blocked", 6: "paused"}

# --- Disclosure ------------------------------------------------------------------------
# kimi auto-loads AGENTS.md and discovers skills from user/project directories and from
# `extra_skill_dirs` in config.toml — which may point ANYWHERE on disk, including outside
# any workspace. Unlike Codex there is no flag to disable third-party connectors.
SKILLS_DISCOVERY_FACT = (
    "Kimi auto-loads the resolved workspace's AGENTS.md and discovers skills from its own "
    "config (including `extra_skill_dirs`, which may point outside the workspace)."
)
SKILLS_DISCOVERY_FACT_FULL = (
    SKILLS_DISCOVERY_FACT
    + " Skill names and descriptions are exposed to the model up front, so that content can "
    "be sent even if your prompt never mentions it. Kimi's built-in skills always load and "
    "cannot be suppressed."
)
SKILLS_ISOLATION_NOTE = (
    "isolation=ignore-skills replaces the auto-discovered user/project skill directories "
    "via --skills-dir, but kimi's BUILT-IN skills still load — verified on 0.35.0. It is a "
    "reduction in exposure, not an elimination."
)

# --- Failure signatures ----------------------------------------------------------------
# Auth. kimi has no `login status` equivalent; auth failures surface from the provider,
# which is user-configured (it may be Moonshot or any OpenAI-compatible endpoint), so these
# stay deliberately generic and conservative.
_AUTH_PATTERNS = (
    re.compile(r"\b401\b|\bunauthorized\b", re.I),
    re.compile(r"\binvalid[_ -]?api[_ -]?key\b", re.I),
    re.compile(r"\bauthentication (failed|error|required)\b", re.I),
    re.compile(r"\bno api key\b|\bapi[_ -]?key (is )?(missing|not set)\b", re.I),
    re.compile(r"\brun `?kimi login`?", re.I),
)
LOGIN_METHOD_CHATGPT = "device-code"  # `kimi login` uses a device-code flow
LOGIN_METHOD_API_KEY = "api key"
LOGIN_STATUS_ARGS = PROVIDER_LIST_ARGS

# Contract drift: kimi/commander rejecting a flag or value the plugin sent.
_DRIFT_PATTERNS = (
    re.compile(r"error: unknown option", re.I),
    re.compile(r"error: option .* argument missing", re.I),
    re.compile(r"\ballowed choices are\b|\binvalid argument\b", re.I),
    re.compile(r"Output format is only supported in prompt mode", re.I),
    re.compile(r"Cannot combine --prompt with", re.I),
    re.compile(r"Cannot use --session without an id", re.I),
    re.compile(r"unknown command", re.I),
)

# A caller-supplied model alias kimi does not know. Verified message on 0.35.0:
#   error: failed to run prompt: Model "X" is not configured in config.toml.
_INVALID_MODEL_PATTERN = re.compile(r'Model ".*?" is not configured in config\.toml', re.I)

# Rate limiting is reported by the configured provider, not by kimi itself.
_RATE_LIMIT_PATTERNS = (
    re.compile(r"\b429\b", re.I),
    re.compile(r"\brate[ _-]?limit(ed|_reached)?\b", re.I),
    re.compile(r"\btoo many requests\b", re.I),
    re.compile(r"\bquota (exceeded|exhausted)\b", re.I),
)
RATE_LIMIT_DEFAULT_BACKOFF_MS = 60_000
_RETRY_AFTER_PATTERNS = (
    re.compile(r"retry[- _]?after[\"']?\s*[:=]\s*[\"']?(\d+(?:\.\d+)?)\s*(ms|s|seconds?)?", re.I),
    re.compile(r"try again in\s+(\d+(?:\.\d+)?)\s*(ms|s|seconds?|minutes?)", re.I),
)

# A rejected reasoning effort. Effort rides an env var, so kimi validates it against the
# model's `support_efforts`; these markers separate "your effort was bad" from real drift.
REASONING_EFFORT_REJECTION_MARKERS = ("support_efforts", "thinking.effort", "effort")
REASONING_EFFORT_TOKEN_PATTERN = re.compile(r"\b(low|medium|high|max)\b", re.I)


def _any(patterns: tuple[re.Pattern[str], ...], texts: tuple[str | None, ...]) -> bool:
    blob = "\n".join(t for t in texts if t)
    if not blob:
        return False
    return any(p.search(blob) for p in patterns)


def is_auth_failure(*texts: str | None) -> bool:
    """Whether the run failed because the configured provider rejected credentials."""
    return _any(_AUTH_PATTERNS, texts)


def is_contract_drift(*texts: str | None) -> bool:
    """Whether kimi rejected a flag or value this plugin sent (fail loudly, never degrade)."""
    return _any(_DRIFT_PATTERNS, texts)


def is_invalid_model(*texts: str | None) -> bool:
    """Whether kimi rejected the requested -m alias as absent from config.toml."""
    return _any((_INVALID_MODEL_PATTERN,), texts)


def is_rate_limited(*texts: str | None) -> bool:
    """Whether the configured provider reported a usage/rate limit."""
    return _any(_RATE_LIMIT_PATTERNS, texts)


def is_reasoning_effort_rejection(*texts: str | None) -> bool:
    """Whether the failure names the effort surface specifically.

    Requires BOTH an effort marker and an effort-looking token, so an unrelated message
    mentioning the word "effort" cannot steal the classification.
    """
    blob = "\n".join(t for t in texts if t)
    if not blob:
        return False
    lowered = blob.lower()
    if not any(m.lower() in lowered for m in REASONING_EFFORT_REJECTION_MARKERS):
        return False
    return REASONING_EFFORT_TOKEN_PATTERN.search(blob) is not None


def parse_retry_after_ms(*texts: str | None) -> int | None:
    """Extract a retry delay in milliseconds, or None when the text carries none.

    Returns 0 faithfully (retry now); callers must test for None rather than falsiness.
    """
    blob = "\n".join(t for t in texts if t)
    if not blob:
        return None
    for pattern in _RETRY_AFTER_PATTERNS:
        m = pattern.search(blob)
        if not m:
            continue
        try:
            value = float(m.group(1))
        except (TypeError, ValueError):  # pragma: no cover - guarded by the pattern
            continue
        unit = (m.group(2) or "s").lower()
        if unit == "ms":
            return int(value)
        if unit.startswith("minute"):
            return int(value * 60_000)
        return int(value * 1000)
    return None
