# Agent working conventions

Conventions for any agent (or human) working in this repository.

## What this is

A Claude Code plugin that calls the Kimi Code CLI via an MCP server. The Python package
is `kimi_in_claude` under `src/`. Generic, CLI-agnostic machinery lives in
`kimi_in_claude/_core/` and is designed for later extraction into a shared `agent-bridge`
package.

- **Rule:** `_core` must never import from its parent package (one-way dependency; this is what
  keeps it extractable).

## Tooling

- Use `uv` for everything: `uv sync`, `uv run pytest`, `uv run <cmd>`. Never pip/poetry.
- **The gate.** This is the repo's single definition of it; every other doc links here rather than
  restating it. A change is not done until it passes:

  ```sh
  uv run ruff check . && uv run ruff format --check . && uv run ty check && uv run pytest
  ```

  If you touched `.github/workflows/`, also run `uv run python scripts/check_github_actions_pinning.py`
  — CI runs it ahead of the four above, and the `prek` pre-commit hook runs it too once installed
  (see below). CI (`.github/workflows/test.yml`) is the authoritative gate and runs all of this on
  every supported Python version.
- Tests use `pytest`; the coverage floor and integration-test markers are defined in Testing below.
- Local Git hooks are configured in `prek.toml` and run via [`prek`](https://prek.j178.dev) (a dev
  dependency). One-time setup: `uv run prek install --prepare-hooks`. Hooks mirror the CI gate —
  pre-commit runs file hygiene + `ruff`/`ty`/Actions-pinning/`uv lock --check`; pre-push runs
  `pytest`; commit-msg validates Conventional Commits via `scripts/check_commit_message.py`. prek
  is a local convenience; CI (`test.yml`) remains the authoritative gate and does not run the
  builtin file-hygiene hooks.
- When changing the allowed commit types or scopes, update `scripts/check_commit_message.py` and
  the Git/PRs section below in the same change — they mirror each other.

## The CLI contract

Every assumption about the `kimi` CLI lives in `src/kimi_in_claude/cli_contract.py` — flags,
sandbox values, version, drift/auth signatures. Guarantee-bearing flags (`ALWAYS_SEND_FLAGS`) are
sent unconditionally and, if rejected, fail loudly as `cli_contract_changed` (zero spend).
Depth-only flags (`HELP_GATED_FLAGS`) are feature-detected and dropped gracefully. When a new
upstream `kimi` minor appears, or the supported version set changes, follow
`docs/UPGRADING-KIMI.md` — it owns the multi-step procedure, which is more than a one-file edit.
`COMPATIBILITY.md` explains what each guarantee is for.

## The result contract

All tools return the envelope in `src/kimi_in_claude/schemas.py`. Bump `FINGERPRINT` whenever the
agent-visible surface changes — any externally observable change to a category in
`FINGERPRINT_COVERS` (same file; the Versioning section has the decision rules). A
committed manifest snapshot (`tests/fixtures/manifest_snapshot.json`, guarded by
`tests/test_manifest.py`) fails CI on any covered change, so the change can't land unreviewed: the
failure directs you to regenerate the fixture
(`uv run python -m kimi_in_claude.manifest > tests/fixtures/manifest_snapshot.json`) and bump
`FINGERPRINT` in the same commit. The snapshot is an **acknowledgment guard** — it surfaces the
drift for review; it does not mechanically force the bump (the snapshot and `FINGERPRINT` are
independently editable, so bumping remains review policy). Record the change in `CHANGELOG.md`.

## Auditing changes with the bundled skills

Maintenance skills live under `.agents/skills/` (mirrored to `.claude/skills/` for Claude Code
discovery). When a change touches the surface below, audit it with the matching skill before landing
— these are quality lenses, not part of [the gate](#tooling):

- **MCP tools, resources, prompts** (schemas, descriptions, the server instructions block) →
  `agent-friendly-mcp`.
- **Instruction-style text** (this file, `skills/` bodies, tool/server descriptions, `commands/`
  slash-command prompts) → `separating-context-from-constraints`.
- **Documentation** (`README`, `docs/`, `CONTRIBUTING`, per-directory context files) →
  `agent-friendly-docs`.

Each skill's own description owns *when* it applies — consult it rather than re-deriving triggers
here. A Claude Code session surfaces these automatically; naming them keeps the expectation explicit
and reachable by any harness that reads this file.

Architectural decisions that affect the agent-visible surface are recorded in `docs/adr/`.

## Versioning

- Semantic Versioning. **Pre-1.0:** a minor bump may change the agent-visible surface (a breaking
  change is a minor, not a major); a patch is a bug fix or internal change. Post-1.0, breaking
  changes are majors.
- Every change is judged on **two independent questions**:
  - **Bumps `FINGERPRINT`?** Yes for any *externally observable* change to a category in
    `FINGERPRINT_COVERS` (`src/kimi_in_claude/schemas.py`) — the discovered value, shape, or
    documented meaning of anything in that tuple. Reference the tuple by name rather than
    re-listing its categories in prose — in this document or **any other doc, template, or comment
    in the repo** (a re-listing drifting out of sync with the code is the exact bug this rule
    exists to prevent, and it has happened: #227 removed one such copy here, while copies in
    `CONTRIBUTING.md`, `docs/UPGRADING-KIMI.md`, and the PR template survived and went stale).
    A refactor that leaves the discovered surface byte-identical does not bump it. Coverage is
    over *contract* semantics, not *release* identity: the per-category carve-outs live on the
    tuple itself and are disclosed to clients on `fingerprint_covers`, which is why an ordinary
    release moves no fingerprint.
  - **Breaking?** Flag it breaking (commit `!`/`BREAKING CHANGE:` footer + the `breaking-change` PR
    label) only when the change is *backward-incompatible* for a client: removing or renaming a
    field/tool/resource/prompt, retyping a field, adding a required input, narrowing an accepted
    value set or enum, changing an output field's meaning under a closed schema, or weakening a
    documented guarantee (an annotation or a promised semantic). Backward-compatible additions and
    wording-only rewords are not breaking.
  - Every breaking change is also a `FINGERPRINT` bump; not every bump is breaking (so #198, a
    wording-only reword, correctly bumped `FINGERPRINT` with no `!`, and #193's `!` was over-flagging
    — the safe direction). Quick reference:

    | Change | Bumps `FINGERPRINT` | Breaking |
    |---|---|---|
    | Add a backward-compatible tool, param, resource, prompt, field, error code, or enum value | Yes | No |
    | Remove/rename/retype a field/tool/resource/prompt, add a required input, or narrow a value set | Yes | Yes |
    | Reword a description/instruction, no guarantee change | Yes | No |
    | Reword text that weakens a documented guarantee | Yes | Yes |
    | Change a `_REPAIR_BY_CODE` machine field (`next_step`'s `RepairStep`, `repair.tool`, `temporary`) | Yes | Per the rules above |
    | Change human-readable `_REPAIR_BY_CODE`/`error.message` prose only | No | No |
    | Internal refactor, discovered surface unchanged | No | No |

    The last two rows are why #197 (repair-hint prose only) bumped neither: that prose ships fresh in
    each error envelope and is absent from the manifest snapshot, so no cached discovery surface
    changed. The exemption is *only* for the human-readable message/prose text — the co-located
    machine-readable repair fields remain part of the discovered surface.
- `CHANGELOG.md` follows Keep a Changelog: land every notable change under `## [Unreleased]`; cutting
  a release moves those entries into a new dated version section and leaves a fresh, empty
  `## [Unreleased]` on top.

## Python support

`requires-python>=3.11`, following SPEC 0 (support Python releases from roughly the last three
years). CI runs the gate on every supported minor. The supported set is defined by the Python trove
classifiers in `pyproject.toml`; a packaging test asserts the CI matrix in
`.github/workflows/test.yml` (the reusable gate `ci.yml` calls) and the
`requires-python` floor stay in lockstep with those classifiers
(so this prose deliberately avoids naming specific versions). Changing the support set is
deliberate: update the classifiers, the CI matrix, and `requires-python` together, and note it in
`CHANGELOG.md`.

## Testing

- TDD: write the failing test first, then the minimal code to pass it.
- Test files mirror the module under test (`tests/test_<module>.py`).
- Every bug fix lands with a regression test that fails before the fix.
- **A new parameter is new API surface, not just new behavior.** Test the documented invariants
  across the parameter's whole domain — the boundary values and the invalid ones — not only the
  values the current callers pass. Red-green covers the behavior you intended; the input domain
  needs its own pass. This matters most in `_core/`, which is written for callers who do not exist
  yet. (#273 added `BoundedCapture(head_bytes=...)` tested only at `None` and `0`, the two values
  its callers used; `head_bytes > max_bytes` then retained ~15x the byte cap while reporting
  `truncated=False`, silently breaking the guarantee stated in that class's own docstring.)
- The **95% coverage floor** is enforced in CI. Live tests that hit the real `kimi` CLI are marked
  `integration` and excluded by default (`uv run pytest -m integration --no-cov`).

## Project status

**Installed from a pinned git tag.** The repo is public at
`https://github.com/briandconnelly/kimi-in-claude`. The plugin is not published to PyPI, so
`.mcp.json` installs it with `uvx --from git+<remote>@v<version>` pinned to a release tag. The
marketplace manifest is separate and still installs from a local checkout (`source: "./"`).

A release therefore moves three things together: the version in `pyproject.toml`, the tag pinned in
`.mcp.json`, and the `v<version>` tag pushed to the remote. `tests/test_packaging.py` guards the
first two against drift. Nothing can guard the third from inside the repo — an unpushed tag makes
`.mcp.json` unresolvable for every user but leaves the working tree green, so push the tag as part
of the release, not after it. There is no issue-claim protocol.

## The safety model, in one place

kimi has **no sandbox and no approvals**. Two controls stand in, and the difference between them
matters when you change anything in `runspace.py`, `kimi.py`, or `cli_contract.py`:

- The read-only `--agent-file` profile is the **enforcing** control for consult and review. If you
  make it possible to launch a read-only tier without it, the tier silently becomes an
  unrestricted agent. `build_exec_command` raises rather than allow this; keep it that way.
- The throwaway worktree is **defense in depth only**. Verified: kimi writes outside it when told
  to. Never describe it as containment, in code comments or in docs.

Read-only prevents modification, not disclosure — kimi's Read tool takes absolute paths.
`COMPATIBILITY.md` lists every non-guarantee; `docs/UPGRADING-KIMI.md` lists the probes that must
be re-run before trusting any of this against a new kimi version.

## Verification expectations

Claims in this repo are meant to be traceable to a probe. When you add or change one:

- Behavioral claims about the `kimi` CLI go in `cli_contract.py` with the captured evidence
  referenced, and the capture lands in `docs/kimi-help/<version>/`.
- Failure-signature patterns use **captured** message text. A classifier tuned to an invented
  phrasing passes its tests and misclassifies in production.
- Before trusting a negative result (no findings, no matches, a clean sweep), confirm the same
  check can surface a known positive. Several bugs here were found exactly that way — the
  orphan sweep reported clean while an independent `pgrep` found a survivor.
