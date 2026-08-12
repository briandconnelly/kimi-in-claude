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
    """Reproduce kimi's process topology, in three layers.

        leader   (own session)            — stands in for the kimi process we killpg
          └── marked   (own session)      — carries `marker` in argv, like kimi's
              └── unmarked (same group)     `bash -c cd '<worktree>' && sleep`

    `marked` being in its OWN session is the whole point: a killpg on the leader's group
    does not reach it, which is the defect the sweep exists to close. `unmarked` carries no
    marker of its own, so it can only be reclaimed by killing `marked`'s process GROUP —
    the second bug found here, where killing matched pids alone stranded it with ppid 1.

    The processes are python rather than shell: a shell may exec-optimize itself away
    (dash and macOS sh differ), dropping the marker and making the test platform-dependent.
    """
    # The marker reaches the leader through the ENVIRONMENT, not its argv. Embedding the
    # grandchild's source in the leader's `-c` string would put the marker in the leader's
    # command line too, so the marker search would match the leader as well and the
    # "unmarked child" assertion below could not distinguish them.
    leader_code = (
        "import os, subprocess, sys, time\n"
        "code = 'import subprocess, time  # ' + os.environ['KIC_TEST_MARKER'] + "
        '\'\\nsubprocess.Popen(["sleep", "120"])\\ntime.sleep(120)\'\n'
        "subprocess.Popen([sys.executable, '-c', code], start_new_session=True)\n"
        "time.sleep(120)\n"
    )
    return subprocess.Popen(
        [sys.executable, "-c", leader_code],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={**os.environ, "KIC_TEST_MARKER": marker},
    )


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _wait_for_orphan(marker: str, timeout: float = 15.0) -> list[int]:
    """Poll until the spawned grandchild appears.

    A fixed sleep raced on slower CI runners: process startup there took longer than the
    0.4s this used to assume, so `find_orphans` legitimately returned [] and the test
    failed for a reason that had nothing to do with the sweep.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = runtime.find_orphans(marker)
        if found:
            return found
        time.sleep(0.1)
    return []


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
    assert _wait_for_orphan(marker), "sweep found nothing — a real orphan would be missed"


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
    orphans = _wait_for_orphan(marker)
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
    marked = _wait_for_orphan(marker)
    assert marked, "precondition failed: no marked process"

    # The `sleep` is a child of the marked process and carries no marker of its own.
    unmarked = []
    for parent in marked:
        unmarked += [
            int(pid)
            for pid in subprocess.run(
                ["pgrep", "-P", str(parent)], capture_output=True, text=True, check=False
            ).stdout.split()
        ]
    unmarked = [pid for pid in unmarked if pid not in marked]
    assert unmarked, "precondition failed: no unmarked child to strand"
    # It must be invisible to a marker search — otherwise this test proves nothing about
    # reclaiming processes the marker cannot find.
    assert not set(unmarked) & set(runtime.find_orphans(marker))

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


def test_the_ps_invocation_disables_width_truncation():
    """GNU ps truncates the command column to terminal width by default, which hides a
    marker further along the command line — the sweep then finds nothing and reports
    success. Invisible on macOS, where BSD ps does not truncate; CI on Linux is what
    caught it. Pin the flag so it cannot be dropped as noise.
    """
    import inspect

    source = inspect.getsource(runtime._ps_matches)
    assert '"-axww"' in source or '"-ww"' in source


def test_find_orphans_sees_a_marker_late_in_a_long_command_line(marker, reap):
    """The behavioral counterpart: a marker past the ~80-column truncation point must
    still be found."""
    padding = "x" * 300
    leader_code = (
        "import os, subprocess, sys, time\n"
        f"code = 'import time  # {padding} ' + os.environ['KIC_TEST_MARKER'] "
        "+ '\\ntime.sleep(120)'\n"
        "subprocess.Popen([sys.executable, '-c', code], start_new_session=True)\n"
        "time.sleep(120)\n"
    )
    subprocess.Popen(
        [sys.executable, "-c", leader_code],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={**os.environ, "KIC_TEST_MARKER": marker},
    )
    assert _wait_for_orphan(marker), "a marker past the truncation point was not found"
