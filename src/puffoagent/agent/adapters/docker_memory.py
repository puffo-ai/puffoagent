"""Docker VM memory probe → resume-heap cap.

``claude --resume`` parses the entire session transcript into Node's V8 heap.
On Docker Desktop (Windows / macOS) the daemon runs in a WSL2 / HyperKit VM
with a fixed memory ceiling — typically 4–8 GiB unless an operator has
raised it via ``%UserProfile%\\.wslconfig`` (Windows) or Docker Desktop →
Resources (macOS). If we set ``--max-old-space-size`` higher than the VM
can actually supply, the kernel returns ``ENOMEM`` to a read syscall long
before V8 itself feels heap-pressured — the exact symptom of
``Failed to resume session: ENOMEM: not enough memory, read``.

So we probe ``docker info`` once at daemon startup, take 50% of the
reported ``MemTotal`` as the resume-time heap cap, and clamp into a sane
range. Operators tune the VM via ``.wslconfig`` and restart Docker, so
the cap is recomputed only on daemon restart — re-probing on every resume
buys nothing and adds latency.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


# Fraction of docker MemTotal we let Node grow into on resume. 50%
# leaves room for the rest of the container's RSS, the kernel page
# cache, and other puffo-* containers competing for the same VM.
RESUME_HEAP_FRACTION = 0.5

# Floor — sessions need a few hundred MB just to bootstrap; on a
# pathologically small VM the cap shouldn't be the thing that kills
# the resume.
MIN_RESUME_HEAP_MB = 1024

# Ceiling — kept at the previously hardcoded value. Past 8 GiB you're
# almost certainly fighting a different bug; transcripts that big
# should be compacted or rotated, not loaded.
MAX_RESUME_HEAP_MB = 8192

# Used when ``docker info`` is unreachable (docker not installed,
# daemon down, cli-local-only deployment on a host without docker).
# Matches Node's ~4 GiB default reasonably and works on any beefy host.
FALLBACK_RESUME_HEAP_MB = 4096


_resume_heap_cap_mb: int | None = None


def compute_resume_heap_cap_mb(total_bytes: int) -> int:
    """Pure helper: 50% of ``total_bytes``, clamped to
    ``[MIN_RESUME_HEAP_MB, MAX_RESUME_HEAP_MB]``. Exposed for tests."""
    target = int(total_bytes * RESUME_HEAP_FRACTION / (1024 * 1024))
    return max(MIN_RESUME_HEAP_MB, min(MAX_RESUME_HEAP_MB, target))


async def _probe_docker_mem_total_bytes() -> int | None:
    """Returns the docker daemon's ``MemTotal`` in bytes, or ``None``
    if docker is unreachable. Format matches
    https://docs.docker.com/reference/cli/docker/system/info/.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "info", "--format", "{{.MemTotal}}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, OSError) as exc:
        logger.warning("docker info: cannot exec docker (%s)", exc)
        return None
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10.0)
    except asyncio.TimeoutError:
        logger.warning("docker info: timed out after 10s; ignoring")
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return None
    if proc.returncode != 0:
        logger.warning(
            "docker info: rc=%s stderr=%s",
            proc.returncode,
            stderr.decode("utf-8", errors="replace").strip()[:200],
        )
        return None
    raw = stdout.decode("utf-8", errors="replace").strip()
    try:
        return int(raw)
    except ValueError:
        logger.warning("docker info: unparseable MemTotal %r", raw)
        return None


async def init_resume_heap_cap() -> int:
    """One-shot startup probe. Sets the module cache and logs the
    chosen cap. Returns the cap in MB. Idempotent: repeated calls
    re-probe and overwrite — useful for tests but never needed in
    production (cap reflects the docker VM, which only changes on
    Docker Desktop restart, which restarts puffoagent anyway).
    """
    global _resume_heap_cap_mb
    total = await _probe_docker_mem_total_bytes()
    if total is None:
        _resume_heap_cap_mb = FALLBACK_RESUME_HEAP_MB
        logger.info(
            "resume-heap cap: docker unavailable, using fallback %d MB",
            FALLBACK_RESUME_HEAP_MB,
        )
        return _resume_heap_cap_mb
    cap = compute_resume_heap_cap_mb(total)
    _resume_heap_cap_mb = cap
    logger.info(
        "resume-heap cap: %d MB (%.0f%% of docker MemTotal=%.2f GiB)",
        cap, RESUME_HEAP_FRACTION * 100, total / (1024 ** 3),
    )
    return cap


def resume_heap_cap_mb() -> int:
    """Cached cap from the last ``init_resume_heap_cap`` call.
    Returns ``FALLBACK_RESUME_HEAP_MB`` if the daemon never primed the
    cache (e.g. a unit test that bypasses startup).
    """
    return (
        _resume_heap_cap_mb
        if _resume_heap_cap_mb is not None
        else FALLBACK_RESUME_HEAP_MB
    )


def _reset_for_tests() -> None:
    """Test-only: clear the cache so the next ``init_resume_heap_cap``
    or ``resume_heap_cap_mb`` call sees an unprimed module."""
    global _resume_heap_cap_mb
    _resume_heap_cap_mb = None
