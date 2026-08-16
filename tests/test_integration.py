"""Live tests that call the real `kimi` CLI. Opt in with:

    uv run pytest -m integration --no-cov

They require kimi to be installed and authenticated (`kimi login`). They spend
tokens, so they are excluded from the default run.
"""

from __future__ import annotations

import pytest

from moonbridge import cli_contract, kimi, server

pytestmark = pytest.mark.integration


def test_status_live():
    res = server.kimi_status()
    assert res["kimi_found"] is True
    assert res["ready"] is True, res["readiness_detail"]


async def test_consult_live(tmp_path):
    res = await server.kimi_consult(
        "Reply concisely in one sentence: what does the DRY principle mean?",
        workspace_root=str(tmp_path),
        timeout_seconds=150,
    )
    assert res["ok"] is True, res.get("error")
    assert res["summary"]
    assert res["meta"]["sandbox"] == "read-only"
    assert res["meta"]["session_id"]


def test_login_status_live():
    logged_in, _ = kimi.login_status()
    assert logged_in is True


async def test_review_changes_live(tmp_path):
    import subprocess

    def g(*a):
        subprocess.run(["git", *a], cwd=tmp_path, check=True, capture_output=True)

    g("init", "-q")
    g("config", "user.email", "t@t.co")
    g("config", "user.name", "t")
    (tmp_path / "m.py").write_text("def f(xs):\n    return xs[0]\n")
    g("add", "-A")
    g("commit", "-qm", "init")
    # Introduce an obvious off-by-one bug.
    (tmp_path / "m.py").write_text(
        "def f(xs):\n"
        "    out = []\n"
        "    for i in range(len(xs) + 1):\n"
        "        out.append(xs[i])\n"
        "    return out\n"
    )
    res = await server.kimi_review_changes(
        scope="working_tree", workspace_root=str(tmp_path), timeout_seconds=150
    )
    assert res["ok"] is True, res.get("error")
    assert res["meta"]["context_summary"]["files_changed"] == 1


async def test_delegate_live(tmp_path):
    import subprocess

    def g(*a):
        subprocess.run(["git", *a], cwd=tmp_path, check=True, capture_output=True)

    g("init", "-q")
    g("config", "user.email", "t@t.co")
    g("config", "user.name", "t")
    (tmp_path / "greet.py").write_text('def greet(n):\n    return "hi " + n\n')
    g("add", "-A")
    g("commit", "-qm", "init")
    before = (tmp_path / "greet.py").read_text()
    res = await server.kimi_delegate(
        "Add a farewell(name) function returning 'bye ' + name to greet.py.",
        workspace_root=str(tmp_path),
        timeout_seconds=180,
    )
    assert res["ok"] is True, res.get("error")
    assert res["diff"]  # a proposed patch came back
    assert (tmp_path / "greet.py").read_text() == before  # live tree untouched
    # worktree cleaned up: only the main worktree remains
    out = subprocess.run(
        ["git", "worktree", "list"], cwd=tmp_path, capture_output=True, text=True, check=True
    ).stdout
    assert out.strip().count("\n") == 0


async def test_delegate_async_live(tmp_path, monkeypatch):
    import subprocess
    import time

    # keep job state out of the user's real cache dir
    monkeypatch.setenv("MOONBRIDGE_STATE_DIR", str(tmp_path / "jobs"))

    def g(*a):
        subprocess.run(["git", *a], cwd=tmp_path, check=True, capture_output=True)

    g("init", "-q")
    g("config", "user.email", "t@t.co")
    g("config", "user.name", "t")
    (tmp_path / "greet.py").write_text('def greet(n):\n    return "hi " + n\n')
    g("add", "-A")
    g("commit", "-qm", "init")
    before = (tmp_path / "greet.py").read_text()

    started = await server.kimi_delegate_async(
        "Add a farewell(name) function returning 'bye ' + name to greet.py.",
        workspace_root=str(tmp_path),
    )
    assert started["ok"] is True, started.get("error")
    job_id = started["job_id"]

    # poll to completion (bounded)
    deadline = time.monotonic() + 240
    status = None
    while time.monotonic() < deadline:
        status = await server.kimi_job_status(job_id, workspace_root=str(tmp_path))
        if status["status"] != "running":
            break
        time.sleep(status.get("poll_after_ms", 1000) / 1000)
    assert status is not None and status["status"] == "done", status

    res = await server.kimi_job_result(job_id, workspace_root=str(tmp_path))
    assert res["ok"] is True, res.get("error")
    assert res["diff"]
    assert res["meta"]["job_id"] == job_id
    assert (tmp_path / "greet.py").read_text() == before  # live tree untouched
    # worktree cleaned up
    out = subprocess.run(
        ["git", "worktree", "list"], cwd=tmp_path, capture_output=True, text=True, check=True
    ).stdout
    assert out.strip().count("\n") == 0

    # consume deletes the record
    await server.kimi_job_consume_result(job_id, workspace_root=str(tmp_path))
    gone = await server.kimi_job_status(job_id, workspace_root=str(tmp_path))
    assert gone["ok"] is False and gone["error"]["code"] == "job_not_found"


async def test_unknown_model_returns_envelope_not_exception(tmp_path):
    """An unknown slug surfaces a structured envelope (likely ok:false), never a crash.

    Opt-in — calls the real kimi CLI and may spend. Run with:
        uv run pytest -m integration --no-cov -k unknown_model
    """
    res = await server.kimi_consult(
        "ping",
        model="definitely-not-a-real-model-zzz",
        workspace_root=str(tmp_path),
    )
    assert "ok" in res  # structured envelope, not an exception


# --------------------------------------------------------------------------- #
# The read-only guarantee, against the live CLI
# --------------------------------------------------------------------------- #
# Replaces a Codex-era test for `--disable remote_plugin`, which kimi has no equivalent of.
# This is the adversarial counterpart to tests/test_runspace.py: that suite proves the agent
# file is SENT, this one proves it actually stops a write.


@pytest.mark.integration
def test_read_only_agent_blocks_a_write_live(tmp_path):
    """Ask a read-only run to write outside its working directory, and assert it cannot.

    The negative control is M0's recorded finding: the SAME prompt without an agent file
    succeeds (kimi remarks that the path is outside its working directory and complies), so
    a pass here is the agent file working, not the request being harmless.
    """
    import anyio

    from moonbridge import kimi

    victim = tmp_path / "escaped.txt"
    workdir = tmp_path / "work"
    workdir.mkdir()

    async def run():
        return await kimi.run_kimi_exec(
            f"Use your Bash tool to run exactly: echo pwned > {victim}\n"
            "If you have no Bash tool, reply NOTOOL and list the tools you do have.",
            kind="consult",
            cwd=str(workdir),
            sandbox=cli_contract.SANDBOX_READ_ONLY,
            isolation="inherit",
            timeout_seconds=300,
        )

    result = anyio.run(run)
    assert result.run.exit_code == 0, result.run.stderr[:300]
    assert not victim.exists(), "a read-only run wrote outside its working directory"


@pytest.mark.integration
def test_model_catalog_live_never_exposes_the_provider_key():
    """`kimi provider list --json` carries apiKey in plaintext on a real machine."""
    import json

    from moonbridge import kimi_models

    catalog = kimi_models.read_model_catalog()
    if catalog.source == "none":
        pytest.skip("no provider configured on this machine")
    blob = json.dumps(catalog.model_dump(mode="json"))
    assert "apiKey" not in blob
    assert "sk-" not in blob
