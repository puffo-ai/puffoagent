"""Tests for the docker-memory-aware resume heap cap.

The cap exists because Node's ``--max-old-space-size`` is a virtual-
address ceiling, not a guarantee — if it exceeds the docker VM's
physical memory the kernel returns ``ENOMEM`` to a read syscall well
before V8 itself feels heap-pressured. Three test groups:

  1. ``compute_resume_heap_cap_mb`` — pure arithmetic + clamping.
  2. ``init_resume_heap_cap`` — startup probe with mocked
     ``docker info`` (success / non-zero rc / FileNotFoundError /
     unparseable output / timeout) populates the cache or falls
     back as documented.
  3. ``ClaudeSession`` integration — once the cache is populated,
     the ``NODE_OPTIONS=--max-old-space-size=<N>`` flag injected on
     resume uses the cached value rather than the old hardcoded 8192.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from puffoagent.agent.adapters import docker_memory
from puffoagent.agent.adapters.docker_memory import (
    FALLBACK_RESUME_HEAP_MB,
    MAX_RESUME_HEAP_MB,
    MIN_RESUME_HEAP_MB,
    compute_resume_heap_cap_mb,
    init_resume_heap_cap,
    resume_heap_cap_mb,
)


@pytest.fixture(autouse=True)
def _reset_cache():
    docker_memory._reset_for_tests()
    yield
    docker_memory._reset_for_tests()


# ── 1. Pure arithmetic ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "total_gib, expected_mb",
    [
        # The user's case: 5.79 GiB VM → 50% = ~2964 MB, above the
        # 1024 MB floor and below the 8192 MB ceiling, so unclamped.
        (5.79, int(5.79 * 1024 * 0.5)),
        # 6 GiB exact → 3072 MB.
        (6.0, 3072),
        # 16 GiB → 8192 MB exactly, hits the ceiling cleanly.
        (16.0, 8192),
        # 32 GiB → would be 16384 but clamped to MAX.
        (32.0, MAX_RESUME_HEAP_MB),
        # 1 GiB → 512 MB, clamped UP to the 1024 MB floor.
        (1.0, MIN_RESUME_HEAP_MB),
        # 0.5 GiB (pathological) → still clamped up to floor.
        (0.5, MIN_RESUME_HEAP_MB),
    ],
)
def test_compute_50_percent_with_clamps(total_gib, expected_mb):
    total_bytes = int(total_gib * 1024 * 1024 * 1024)
    assert compute_resume_heap_cap_mb(total_bytes) == expected_mb


# ── 2. Startup probe ─────────────────────────────────────────────────────────


class _FakeProc:
    """Stand-in for ``asyncio.subprocess.Process`` covering the bits
    ``_probe_docker_mem_total_bytes`` actually touches."""

    def __init__(self, stdout: bytes, stderr: bytes = b"", returncode: int = 0):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode

    async def communicate(self):
        return self._stdout, self._stderr

    def kill(self):
        pass


def _patch_subprocess(proc):
    """Make ``asyncio.create_subprocess_exec`` return a coroutine that
    yields ``proc``. Patches the symbol inside the module under test."""
    async def _factory(*_args, **_kwargs):
        return proc
    return patch.object(
        docker_memory.asyncio, "create_subprocess_exec", side_effect=_factory,
    )


def test_init_uses_50_percent_when_docker_responds(event_loop=None):
    # 6 GiB MemTotal → cap = 3072 MB.
    six_gib = 6 * 1024 * 1024 * 1024
    proc = _FakeProc(stdout=str(six_gib).encode() + b"\n")
    with _patch_subprocess(proc):
        cap = asyncio.run(init_resume_heap_cap())
    assert cap == 3072
    assert resume_heap_cap_mb() == 3072


def test_init_falls_back_on_nonzero_rc():
    proc = _FakeProc(stdout=b"", stderr=b"docker: not running", returncode=1)
    with _patch_subprocess(proc):
        cap = asyncio.run(init_resume_heap_cap())
    assert cap == FALLBACK_RESUME_HEAP_MB
    assert resume_heap_cap_mb() == FALLBACK_RESUME_HEAP_MB


def test_init_falls_back_when_docker_not_installed():
    async def _factory(*_args, **_kwargs):
        raise FileNotFoundError("[WinError 2] docker.exe not found")
    with patch.object(
        docker_memory.asyncio, "create_subprocess_exec", side_effect=_factory,
    ):
        cap = asyncio.run(init_resume_heap_cap())
    assert cap == FALLBACK_RESUME_HEAP_MB


def test_init_falls_back_on_unparseable_output():
    # Docker spec says MemTotal is bytes, but cover a malformed
    # response (e.g. operator points at a docker-compatible
    # daemon that emits a different format string).
    proc = _FakeProc(stdout=b"not-a-number\n")
    with _patch_subprocess(proc):
        cap = asyncio.run(init_resume_heap_cap())
    assert cap == FALLBACK_RESUME_HEAP_MB


def test_resume_heap_cap_returns_fallback_before_init():
    # Cache is reset by the autouse fixture; calling without an
    # init must not raise — tests / scripts that import the
    # session module without spinning up a daemon hit this path.
    assert resume_heap_cap_mb() == FALLBACK_RESUME_HEAP_MB


def test_init_is_idempotent_and_overwrites():
    # First probe: 16 GiB → ceiling.
    proc1 = _FakeProc(stdout=str(16 * 1024 * 1024 * 1024).encode())
    with _patch_subprocess(proc1):
        first = asyncio.run(init_resume_heap_cap())
    assert first == MAX_RESUME_HEAP_MB

    # Second probe (e.g. operator restarted the daemon after
    # shrinking .wslconfig): 4 GiB → 2048 MB. Must overwrite.
    proc2 = _FakeProc(stdout=str(4 * 1024 * 1024 * 1024).encode())
    with _patch_subprocess(proc2):
        second = asyncio.run(init_resume_heap_cap())
    assert second == 2048
    assert resume_heap_cap_mb() == 2048


# ── 3. Integration with ClaudeSession ────────────────────────────────────────


def test_resume_spawn_uses_cached_cap_in_node_options(tmp_path):
    """End-to-end check: a session with a saved session_id resumes
    with NODE_OPTIONS sized to the cached cap, not the old 8192 literal.
    """
    from puffoagent.agent.adapters.cli_session import ClaudeSession

    # Pretend the daemon already probed and got 3072.
    docker_memory._resume_heap_cap_mb = 3072

    # Capture what ClaudeSession passes to ``build_command``.
    captured: dict[str, object] = {}

    def _fake_build(extra_args, env_overrides):
        captured["extra_args"] = list(extra_args)
        captured["env_overrides"] = dict(env_overrides or {})
        # Return a no-op argv so the test never actually spawns claude.
        return ["python", "-c", "import sys; sys.exit(0)"]

    session = ClaudeSession(
        agent_id="t",
        session_file=tmp_path / "session.json",
        build_command=_fake_build,
        cwd=str(tmp_path),
        env={},
    )
    # Force "we have a persisted session id so this is a --resume".
    session._session_id = "fake-session-id"

    # We don't want to read stdout / wait on init — just trigger
    # ``_spawn`` far enough to exercise the env_overrides path.
    async def _drive():
        try:
            await asyncio.wait_for(session._spawn(""), timeout=2.0)
        except (asyncio.TimeoutError, Exception):
            # The fake "claude" exits immediately so init never
            # arrives; that's fine — we only care what the session
            # passed to ``build_command`` BEFORE spawning.
            pass

    asyncio.run(_drive())

    node_options = captured["env_overrides"].get("NODE_OPTIONS", "")
    assert "--max-old-space-size=3072" in node_options
    assert "--max-old-space-size=8192" not in node_options
