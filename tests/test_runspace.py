"""The isolation contract every tier goes through.

kimi has no read-only sandbox, so two properties carry the weight and both are asserted
here rather than assumed:

* consult and review are launched with the read-only agent profile, which is what actually
  removes Bash and Write from the model's tool schema;
* whatever a run does, the caller's real working tree is unchanged afterwards.

The kimi subprocess is faked at the `runtime.run_async` seam, so these run without the CLI
and without model spend. The live counterpart — kimi genuinely refusing to write — is in
tests/test_integration.py behind `-m integration`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from pontifex.core import worktree
from pontifex.core.runtime import CommandRun

from moonbridge import cli_contract, config, kimi, runspace
from moonbridge.schemas import Meta

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _meta() -> Meta:
    return Meta(
        cwd="/x",
        tier="consult",
        sandbox="read-only",
        isolation="inherit",
        timeout_seconds=60,
        elapsed_ms=0,
    )


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    run = lambda *a: subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True)  # noqa: E731
    run("init", "-q")
    run("config", "user.email", "t@t.t")
    run("config", "user.name", "T")
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    run("add", "-A")
    run("commit", "-qm", "init")
    # Leave an uncommitted change: it must survive a consult untouched.
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n\n# wip\n")
    return repo


def _tree_state(repo: Path) -> tuple[str, str]:
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout
    return status, (repo / "calc.py").read_text()


@pytest.fixture
def fake_kimi(monkeypatch):
    """Capture the command kimi would have run, and emit a plausible stream-json reply."""
    calls: list[dict] = []

    async def _fake_run_async(cmd, cwd, timeout_seconds, stdin_text=None, **kwargs):
        calls.append({"cmd": cmd, "cwd": cwd})
        stdout = (
            '{"role":"meta","type":"system.version","version":"0.35.0"}\n'
            '{"role":"assistant","content":"The approach is sound."}\n'
            '{"role":"meta","type":"session.resume_hint","session_id":"session_abc"}\n'
        )
        return CommandRun(stdout, "", 0, 12, False)

    monkeypatch.setattr("pontifex.core.runtime.run_async", _fake_run_async)
    return calls


async def test_consult_runs_under_the_read_only_agent(tmp_path, fake_kimi):
    """The agent file is the enforcing control — not the worktree. It must always be sent."""
    repo = _repo(tmp_path)
    outcome = await runspace.run_isolated(
        "is this sound?",
        kind="consult",
        cwd=str(repo),
        meta=_meta(),
        sandbox=cli_contract.SANDBOX_READ_ONLY,
        isolation="inherit",
        timeout_seconds=60,
        model=None,
        git_timeout=60,
        capture_diff=False,
    )
    assert outcome.error is None
    cmd = fake_kimi[0]["cmd"]
    assert cli_contract.AGENT_FILE_FLAG in cmd, "read-only run was launched without the agent file"


async def test_consult_leaves_the_real_working_tree_byte_identical(tmp_path, fake_kimi):
    repo = _repo(tmp_path)
    before = _tree_state(repo)
    await runspace.run_isolated(
        "is this sound?",
        kind="consult",
        cwd=str(repo),
        meta=_meta(),
        sandbox=cli_contract.SANDBOX_READ_ONLY,
        isolation="inherit",
        timeout_seconds=60,
        model=None,
        git_timeout=60,
        capture_diff=False,
    )
    assert _tree_state(repo) == before


async def test_consult_does_not_run_in_the_real_repo(tmp_path, fake_kimi):
    # If cwd were the live repo, the read-only profile would be the ONLY thing protecting
    # it. Defense in depth means the run happens somewhere disposable.
    repo = _repo(tmp_path)
    await runspace.run_isolated(
        "q",
        kind="consult",
        cwd=str(repo),
        meta=_meta(),
        sandbox=cli_contract.SANDBOX_READ_ONLY,
        isolation="inherit",
        timeout_seconds=60,
        model=None,
        git_timeout=60,
        capture_diff=False,
    )
    assert Path(fake_kimi[0]["cwd"]).resolve() != repo.resolve()


async def test_the_worktree_is_removed_afterwards(tmp_path, fake_kimi):
    repo = _repo(tmp_path)
    await runspace.run_isolated(
        "q",
        kind="consult",
        cwd=str(repo),
        meta=_meta(),
        sandbox=cli_contract.SANDBOX_READ_ONLY,
        isolation="inherit",
        timeout_seconds=60,
        model=None,
        git_timeout=60,
        capture_diff=False,
    )
    assert not Path(fake_kimi[0]["cwd"]).exists()


async def test_handshake_dir_never_reaches_a_captured_diff(tmp_path, monkeypatch):
    """The prompt/answer files live inside the worktree; they must not become a delegate diff."""

    async def _writing_run(cmd, cwd, timeout_seconds, stdin_text=None, **kwargs):
        # Behave like a delegate: make a real edit alongside the handshake files.
        (Path(cwd) / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\ndef mul(a,b):\n    return a*b\n"
        )
        return CommandRun('{"role":"assistant","content":"done"}\n', "", 0, 5, False)

    monkeypatch.setattr("pontifex.core.runtime.run_async", _writing_run)
    repo = _repo(tmp_path)
    outcome = await runspace.run_isolated(
        "add mul",
        kind="consult",
        cwd=str(repo),
        meta=Meta(
            cwd="/x",
            tier="propose",
            sandbox="workspace-write",
            isolation="inherit",
            timeout_seconds=60,
            elapsed_ms=0,
        ),
        sandbox=cli_contract.SANDBOX_WORKSPACE_WRITE,
        isolation="inherit",
        timeout_seconds=60,
        model=None,
        git_timeout=60,
        capture_diff=True,
    )
    assert outcome.error is None
    assert "def mul" in outcome.diff, "the real edit is missing from the diff"
    assert cli_contract.HANDSHAKE_DIR_NAME not in outcome.diff


async def test_non_repo_consult_is_isolated_and_discloses_it(tmp_path, fake_kimi):
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    meta = _meta()
    outcome = await runspace.run_isolated(
        "q",
        kind="consult",
        cwd=str(plain),
        meta=meta,
        sandbox=cli_contract.SANDBOX_READ_ONLY,
        isolation="inherit",
        timeout_seconds=60,
        model=None,
        git_timeout=60,
        capture_diff=False,
        allow_non_repo=True,
    )
    assert outcome.error is None
    assert runspace.NO_REPO_WARNING in meta.security_warnings
    assert Path(fake_kimi[0]["cwd"]).resolve() != plain.resolve()


async def test_non_repo_consult_without_allow_non_repo_errors(tmp_path, fake_kimi):
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    outcome = await runspace.run_isolated(
        "q",
        kind="consult",
        cwd=str(plain),
        meta=_meta(),
        sandbox=cli_contract.SANDBOX_READ_ONLY,
        isolation="inherit",
        timeout_seconds=60,
        model=None,
        git_timeout=60,
        capture_diff=False,
    )
    assert outcome.error is not None
    assert outcome.error["error"]["code"] == "not_a_git_repo"


def test_read_only_agent_document_grants_no_write_or_shell():
    doc = kimi.read_only_agent_document()
    assert "Bash" not in doc
    assert "Write" not in doc
    assert "Edit" not in doc
    for tool in cli_contract.READ_ONLY_AGENT_TOOLS:
        assert tool in doc


def test_handshake_dir_name_matches_the_worktree_exclude():
    """pontifex.core cannot know this bridge's handshake dir, so the exclusion lives in
    config.WORKTREE_CONFIG.extra_excludes with the name written out literally. If it drifts
    from cli_contract.HANDSHAKE_DIR_NAME, the prompt and answer files silently start
    appearing in every delegate diff."""
    assert any(
        cli_contract.HANDSHAKE_DIR_NAME in pattern
        for pattern in config.WORKTREE_CONFIG.extra_excludes
    )


def test_is_git_repo_negative_control(tmp_path):
    # Guards the fallback branch: if this returned True everywhere, allow_non_repo would
    # never engage and a non-repo consult would fail instead of degrading.
    assert not worktree.is_git_repo(str(tmp_path), timeout=30)
