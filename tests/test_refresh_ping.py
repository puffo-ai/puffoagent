"""Unit tests for refresh_ping orchestration in base.Adapter.

Covers the pieces that tend to drift in silence:
  - ``expiresAt`` threshold (skip when fresh, run when near expiry)
  - Daemon-wide mutex (concurrent callers don't dogpile the OAuth
    refresh endpoint — late arrivals no-op rather than queue)
  - Post-lock re-check (another agent refreshed during the wait)
  - Logging (before / after / "didn't advance" warning)
"""

import asyncio
import logging

import pytest

from puffoagent.agent.adapters import base


class _Fixture(base.Adapter):
    """Concrete Adapter for tests — script ``expires_in`` values and
    observe the interaction with ``_run_refresh_oneshot``.
    """

    def __init__(
        self,
        expires_queue: list[int | None],
        run_oneshot_delay: float = 0.0,
    ):
        # Each call to _credentials_expires_in_seconds pops from
        # this list. Lets a single test assert on the before-check,
        # re-check-after-lock, and after-refresh values separately.
        self._queue = list(expires_queue)
        self._run_oneshot_delay = run_oneshot_delay
        self.oneshot_calls = 0

    async def run_turn(self, ctx):
        raise NotImplementedError

    def _credentials_expires_in_seconds(self):
        if not self._queue:
            return None
        return self._queue.pop(0)

    async def _run_refresh_oneshot(self):
        self.oneshot_calls += 1
        if self._run_oneshot_delay:
            await asyncio.sleep(self._run_oneshot_delay)


def _run(coro):
    return asyncio.run(coro)


# ── Threshold gate ───────────────────────────────────────────────────────────


class TestThresholdGate:
    def test_fresh_skips_refresh(self):
        # Token still has 2h left → no refresh, no oneshot call.
        adapter = _Fixture(expires_queue=[2 * 3600])
        _run(adapter.refresh_ping())
        assert adapter.oneshot_calls == 0

    def test_near_expiry_triggers_refresh(self):
        # Token has 5 min left (under the 15-min threshold).
        # expires_in values consumed: before-gate, after-lock-recheck,
        # after-refresh. Third value simulates a successful refresh
        # bumping the expiry back to 2h.
        adapter = _Fixture(expires_queue=[5 * 60, 5 * 60, 2 * 3600])
        _run(adapter.refresh_ping())
        assert adapter.oneshot_calls == 1

    def test_at_exact_threshold_triggers_refresh(self):
        # Boundary check: expires_in == threshold should refresh.
        # (Logic uses `> threshold` for "skip", so `==` falls
        # through to the refresh path.)
        t = base.CREDENTIAL_REFRESH_BEFORE_EXPIRY_SECONDS
        adapter = _Fixture(expires_queue=[t, t, t * 4])
        _run(adapter.refresh_ping())
        assert adapter.oneshot_calls == 1

    def test_none_from_hook_shortcircuits(self):
        # SDK / chat-only adapters return None → no refresh.
        adapter = _Fixture(expires_queue=[None])
        _run(adapter.refresh_ping())
        assert adapter.oneshot_calls == 0


# ── Mutex ────────────────────────────────────────────────────────────────────


class TestMutex:
    def test_concurrent_agents_only_one_refresh(self):
        """Two agents both tick past the threshold at the same time.
        Only the first to acquire the lock actually refreshes; the
        other sees ``_REFRESH_LOCK.locked()`` and no-ops.
        """
        async def scenario():
            # Each adapter will be asked for expires_in multiple
            # times: once at the initial gate. The winner also gets
            # asked for the post-lock recheck and post-refresh
            # value; the loser short-circuits before any re-check.
            a = _Fixture(
                expires_queue=[60, 60, 2 * 3600],
                run_oneshot_delay=0.05,  # hold the lock briefly
            )
            b = _Fixture(expires_queue=[60])
            # Schedule a first so it grabs the lock first.
            task_a = asyncio.create_task(a.refresh_ping())
            await asyncio.sleep(0.005)  # let a enter the lock
            task_b = asyncio.create_task(b.refresh_ping())
            await asyncio.gather(task_a, task_b)
            return a, b

        a, b = _run(scenario())
        assert a.oneshot_calls == 1
        assert b.oneshot_calls == 0, (
            "Second agent should have no-oped while the first held the lock"
        )

    def test_sequential_agents_both_refresh_if_both_stale(self):
        # Lock is released between calls — the second agent sees
        # the file still stale (contrived, but validates the
        # recheck path) and refreshes too.
        async def scenario():
            a = _Fixture(expires_queue=[60, 60, 2 * 3600])
            b = _Fixture(expires_queue=[60, 60, 2 * 3600])
            await a.refresh_ping()
            await b.refresh_ping()
            return a, b

        a, b = _run(scenario())
        assert a.oneshot_calls == 1
        assert b.oneshot_calls == 1

    def test_recheck_after_lock_skips_if_another_just_refreshed(self):
        # An adapter ticks past the threshold, but by the time it
        # acquires the lock the file has been refreshed (simulated
        # via a fresh expires_in on the re-check). The refresh
        # one-shot is NOT called.
        adapter = _Fixture(expires_queue=[
            60,           # before-gate: near expiry
            2 * 3600,     # after-lock recheck: already refreshed
        ])
        _run(adapter.refresh_ping())
        assert adapter.oneshot_calls == 0


# ── Logging ──────────────────────────────────────────────────────────────────


class TestLogging:
    def test_successful_refresh_logs_before_and_after(self, caplog):
        adapter = _Fixture(expires_queue=[60, 60, 2 * 3600])
        with caplog.at_level(logging.INFO, logger="puffoagent.agent.adapters.base"):
            _run(adapter.refresh_ping())
        messages = [r.message for r in caplog.records]
        assert any("credentials expire in 60s — running refresh ping" in m for m in messages)
        assert any("credentials refreshed: expires in 7200s (was 60s)" in m for m in messages)

    def test_refresh_that_doesnt_advance_expiry_warns(self, caplog):
        # Refresh ran (oneshot called) but expiry didn't move forward
        # — something is wrong, we log a warning to surface it.
        adapter = _Fixture(expires_queue=[60, 60, 60])
        with caplog.at_level(logging.WARNING, logger="puffoagent.agent.adapters.base"):
            _run(adapter.refresh_ping())
        assert any(
            "refresh_ping ran but token expiry didn't advance" in r.message
            for r in caplog.records
        )

    def test_fresh_token_does_not_log_info(self, caplog):
        # When the token is fresh we skip at DEBUG level — INFO
        # should stay clean.
        adapter = _Fixture(expires_queue=[2 * 3600])
        with caplog.at_level(logging.INFO, logger="puffoagent.agent.adapters.base"):
            _run(adapter.refresh_ping())
        assert not any(
            "running refresh ping" in r.message for r in caplog.records
        )
