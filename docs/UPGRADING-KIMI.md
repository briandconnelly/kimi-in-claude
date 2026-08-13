# Upgrading the supported `kimi` version

`cli_contract.py` is the single source of truth for what this server assumes about the `kimi`
CLI. Moving to a new upstream version is **not** a one-line edit to `SUPPORTED_VERSIONS`: the
assumptions have to be re-verified by running the binary, because several of them are behaviors
that no `--help` output states.

Work through this in order. Do not predict a probe's result — run it and paste the output.

## 1. Capture the new surface

```sh
mkdir -p docs/kimi-help/<VERSION>
kimi --version > docs/kimi-help/<VERSION>/kimi-version.txt
kimi --help    > docs/kimi-help/<VERSION>/kimi-help.txt
```

Diff against the previous version's captures. A flag this server sends that has been renamed or
removed is a release blocker, not a warning.

## 2. Re-run the behavioral probes

`--help` does not tell you any of the following. Each one is load-bearing; record the result in
`docs/kimi-help/<VERSION>/M0-FINDINGS.md`.

| # | Probe | What breaks if it changed |
|---|---|---|
| 1 | Run a task under an `--agent-file` declaring `tools: [Read, Glob, Grep]`, then ask it to run a shell command that writes a file. | **The read-only guarantee.** If a write succeeds, consult and review are no longer read-only — stop and do not ship. |
| 2 | Ask that same read-only agent to Read an absolute path outside the workspace. | The confidentiality wording in `READ_ONLY_CONFIDENTIALITY_LIMIT`. If it is now refused, the docs understate the protection and should be corrected. |
| 3 | Run with a deliberately bogus `KIMI_MODEL_THINKING_EFFORT`. | If kimi now *rejects* it instead of ignoring it, `classify_failure` may want a branch again, and the local pre-spend guard's rationale changes. |
| 4 | Ask a run to write to an absolute path outside its worktree. | Whether the worktree is still only defense in depth. If it is now contained, several documents overstate the risk. |
| 5 | Start a run whose shell command sleeps, then kill kimi's process group. | The orphan sweep. If children now die with the group, `sweep_orphans` becomes belt-and-braces rather than required. |
| 6 | Binary-search the argv prompt length until it fails. | `MAX_ARGV_PROMPT_CHARS`, and whether the failure is still an ugly Node `RangeError`. |
| 7 | Run with `--output-format stream-json` and confirm the line shapes. | Answer extraction and metadata parsing. |
| 8 | `kimi -p hi --model <nonexistent>`. | The `invalid_model` classifier string. |

## 3. Update the contract

Edit `cli_contract.py` only after the probes are recorded:

- `SUPPORTED_VERSIONS`
- any flag constant whose spelling changed — remembering that `preflight` parses **long** flags
  only, so a short form there is silently never "supported"
- the failure-signature patterns, using **captured** message text rather than invented phrasings
- the disclosure constants if kimi's context-loading changed

## 4. Re-run the gate and the live tests

```sh
uv run ruff check . && uv run ruff format --check . && uv run ty check && uv run pytest
uv run pytest -m integration --no-cov
```

## 5. Bump `FINGERPRINT` if the agent-visible surface moved

Any externally observable change to a category in `FINGERPRINT_COVERS` moves the fingerprint, and
the manifest snapshot must be regenerated in the same commit:

```sh
uv run python -m moonbridge.manifest > tests/fixtures/manifest_snapshot.json
```

`tests/test_manifest.py` also pins the snapshot's hash; update it alongside.

## 6. Update the docs that carry claims

`README.md`, `SECURITY.md`, and `COMPATIBILITY.md` all state verified behavior. If a probe result
changed, the wording changes with it — an out-of-date safety claim is worse than none.
