"""Orphan sweep: reclaim processes that survive a process-group kill.

Regression coverage for the M0-7 finding. kimi's Bash tool spawns each command in its OWN
process group, reparented to init, so `killpg` on kimi's group leaves those children
running indefinitely. Verified on kimi-code 0.35.0: after killing kimi's group (pid 3816),
a `sleep 240` survived as pid 3828 / pgid 3828 / ppid 1.

These tests spawn a plain shell rather than kimi — the defect is about process topology, not
about kimi — so they run without the CLI and without model spend.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
import uuid

import pytest

from kimi_in_claude._core import runtime


def _spawn_detached_child(marker: str) -> subprocess.Popen:
    """Reproduce kimi's process topology.

    The leader runs in its own session (as the plugin spawns kimi), and it starts a
    grandchild in a SEPARATE session whose command line embeds `marker` — mirroring
    kimi's Bash tool, which spawns `/bin/bash -c cd '<worktree>' && ...` with its own
    pgid so it survives a killpg on the leader's group.

    A grandchild in the leader's own group would be reclaimed by killpg, which is exactly
    the case that does NOT need a sweep, so the test would prove nothing.
    """
    leader = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                # `cd ... && sleep` is a COMPOUND command, so the shell does not
                # exec-optimize itself away and its argv keeps the marker — the same
                # reason kimi's real strays stay visible as
                # `/bin/bash -c cd '<worktree>' && ...`. A bare `sh -c "sleep 120"`
                # would exec into `sleep` and lose the marker entirely.
                "import subprocess, time\n"
                f"subprocess.Popen(['/bin/sh', '-c', \"cd /tmp && sleep 120 # {marker}\"],"
                " start_new_session=True)\n"
                "time.sleep(120)\n"
            ),
        ],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return leader


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _wait_gone(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return not _alive(pid)


@pytest.fixture
def marker() -> str:
    """A unique marker standing in for the per-run worktree path."""
    return f"kic-worktree-{uuid.uuid4().hex}"


@pytest.fixture
def reap(marker):
    """Reclaim anything this test spawned.

    Killing only the leader's process group is NOT enough — that is the whole defect these
    tests demonstrate — so teardown uses the sweep itself. Without this the suite leaks a
    live `sleep` per test, which is how it was caught.
    """
    yield
    with contextlib.suppress(ValueError):
        runtime.sweep_orphans(marker, grace_seconds=0.2)


def test_find_orphans_locates_a_process_by_marker(marker, reap):
    _spawn_detached_child(marker)
    time.sleep(0.4)
    assert runtime.find_orphans(marker), "sweep found nothing — a real orphan would be missed"


def test_find_orphans_is_empty_for_an_unused_marker():
    # Negative control: proves a positive result above is not a match-everything bug.
    assert runtime.find_orphans(f"kic-worktree-{uuid.uuid4().hex}") == []


def test_find_orphans_never_returns_our_own_pid(marker):
    # The sweeping process itself can carry the marker in its argv (the worktree path is
    # passed to kimi via --agent-file). Killing ourselves would take down the MCP server.
    assert os.getpid() not in runtime.find_orphans(marker)


def test_sweep_orphans_kills_a_survivor_of_killpg(marker, reap):
    """The M0-7 scenario end to end: killpg leaves the child, the sweep reclaims it."""
    proc = _spawn_detached_child(marker)
    time.sleep(0.4)
    orphans = runtime.find_orphans(marker)
    assert orphans, "precondition failed: no orphan to reclaim"

    # Kill only the leader's group, exactly as _kill_group does.
    with _suppress():
        os.killpg(proc.pid, signal.SIGKILL)
    proc.wait(timeout=5)
    time.sleep(0.3)

    # The defect itself: killpg on the leader's group does NOT reclaim the grandchild.
    # If this ever stops holding, the sweep is no longer needed and this test should fail
    # loudly rather than silently pass for the wrong reason.
    assert runtime.find_orphans(marker), "killpg already reclaimed it; sweep unexercised"

    killed = runtime.sweep_orphans(marker, grace_seconds=0.5)
    assert killed, "sweep reported nothing killed"
    for pid in killed:
        assert _wait_gone(pid), f"pid {pid} survived the sweep"
    assert runtime.find_orphans(marker) == []


def test_sweep_orphans_reclaims_an_unmarked_grandchild(marker, reap):
    """A matched process's own children do NOT carry the marker.

    Regression for a gap found by an independent `pgrep` check during the live kimi test:
    the sweep killed the marked shell but left its `sleep` child alive with ppid 1, while
    a marker-only search reported the sweep clean. Matching on the marker and killing only
    that pid is not enough — the whole process group has to go.
    """
    proc = _spawn_detached_child(marker)
    time.sleep(0.5)
    marked = runtime.find_orphans(marker)
    assert marked, "precondition failed: no marked process"

    # The `sleep` grandchild is a child of the marked shell and carries no marker itself.
    unmarked = [
        int(pid)
        for pid in subprocess.run(
            ["pgrep", "-P", str(marked[0])], capture_output=True, text=True, check=False
        ).stdout.split()
    ]
    assert unmarked, "precondition failed: marked shell has no child to strand"

    with _suppress():
        os.killpg(proc.pid, signal.SIGKILL)
    proc.wait(timeout=5)

    runtime.sweep_orphans(marker, grace_seconds=0.5)
    for pid in unmarked:
        assert _wait_gone(pid), f"unmarked grandchild {pid} survived the sweep"


def test_sweep_orphans_never_kills_our_own_process_group():
    # killpg on our own group would take down the MCP server and every sibling job.
    assert os.getpgrp() not in runtime._orphan_process_groups(f"kic-{uuid.uuid4().hex}")


def test_sweep_orphans_is_a_noop_without_a_marker():
    # An empty marker would match every process on the machine; it must refuse.
    with pytest.raises(ValueError):
        runtime.sweep_orphans("")


def test_sweep_orphans_refuses_a_short_marker():
    # A short marker is not unique enough to be safe to kill on.
    with pytest.raises(ValueError):
        runtime.sweep_orphans("tmp")


class _suppress:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return exc_type in (ProcessLookupError, PermissionError)
