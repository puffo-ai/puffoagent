"""Tests for the defensive ``delete_team`` -> auto-archive pathway.

When an operator deletes a Puffo space (Mattermost team) server-side,
three things happen in order:

  1. Mattermost broadcasts a ``delete_team`` websocket event to every
     member of the deleted team, including any bot users.

  2. The agent worker's MM-client dispatches that event to an
     ``on_team_deleted(team_id)`` callback; if ``team_id`` matches the
     bot's own team the worker drops an ``archive.flag`` sentinel into
     its agent dir.

  3. The daemon reconciler picks up the flag on its next tick, stops
     the worker, and moves the agent dir into ``archived/``.

This file exercises (1) and (3) directly — (2) is the closure in
worker.py; we cover its state effect by confirming the flag path
lands where the daemon expects.

The /aiagents owner=me sync already archives local agents that
disappear server-side (tested elsewhere). This file covers the
belt-and-suspenders path that handles the case where the server-side
cascade is delayed, skipped, or broken.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from puffoagent.agent.mattermost_client import MattermostClient
from puffoagent.portal.state import archive_flag_path


def _run(coro):
    return asyncio.run(coro)


# ── (1) MM client dispatches delete_team events ──────────────────────────────


def test_mm_client_dispatches_delete_team_to_handler():
    client = MattermostClient(
        url="http://localhost:8065",
        token="bot-token",
        team_name="core-3",
    )
    received: list[str] = []

    async def _handler(team_id: str) -> None:
        received.append(team_id)

    client.set_team_deleted_handler(_handler)

    event = {
        "event": "delete_team",
        "data": {"team_id": "team-123"},
        "broadcast": {"team_id": "team-123"},
    }
    _run(client._handle_event(event, on_message=None, session=None))

    assert received == ["team-123"]


def test_mm_client_ignores_delete_team_with_no_team_id():
    """Malformed events shouldn't crash the handler loop."""
    client = MattermostClient(url="http://x", token="t", team_name="x")
    received: list[str] = []

    async def _handler(team_id: str) -> None:
        received.append(team_id)

    client.set_team_deleted_handler(_handler)
    _run(client._handle_event(
        {"event": "delete_team", "data": {}},
        on_message=None, session=None,
    ))
    assert received == []


def test_mm_client_delete_team_with_no_registered_handler_is_noop():
    """A bot wired up before the handler is registered (e.g., a
    future code path) shouldn't crash when it receives the event."""
    client = MattermostClient(url="http://x", token="t", team_name="x")
    # No set_team_deleted_handler call.
    _run(client._handle_event(
        {"event": "delete_team", "data": {"team_id": "t1"}},
        on_message=None, session=None,
    ))
    # No assertion beyond "didn't raise".


def test_mm_client_handler_exception_is_caught_not_propagated():
    """A misbehaving handler shouldn't crash the listen loop."""
    client = MattermostClient(url="http://x", token="t", team_name="x")

    async def _bad_handler(team_id: str) -> None:
        raise RuntimeError("boom")

    client.set_team_deleted_handler(_bad_handler)
    # Should NOT raise.
    _run(client._handle_event(
        {"event": "delete_team", "data": {"team_id": "t1"}},
        on_message=None, session=None,
    ))


def test_mm_client_non_team_events_still_flow_through():
    """Don't short-circuit other event types when delete_team is
    registered."""
    client = MattermostClient(url="http://x", token="t", team_name="x")
    team_calls: list[str] = []
    posted_calls: list[dict] = []

    async def _team_handler(team_id: str) -> None:
        team_calls.append(team_id)

    async def _on_message(**kwargs):
        posted_calls.append(kwargs)

    client.set_team_deleted_handler(_team_handler)

    # Unknown event type — should no-op.
    _run(client._handle_event(
        {"event": "channel_viewed"}, on_message=_on_message, session=None,
    ))
    assert team_calls == []
    assert posted_calls == []


# ── archive_flag_path lives where the daemon looks for it ────────────────────


def test_archive_flag_path_matches_daemon_expectation(tmp_path, monkeypatch):
    """The worker writes this path; the daemon reads it. They MUST
    agree — hard to catch in code review, easy to catch in a test.
    """
    monkeypatch.setenv("PUFFOAGENT_HOME", str(tmp_path))
    path = archive_flag_path("my-agent")

    expected = tmp_path / "agents" / "my-agent" / ".puffoagent" / "archive.flag"
    assert path == expected


# ── (3) Daemon archives the agent when the flag is present ───────────────────


def test_daemon_archive_on_flag_moves_dir_to_archived(tmp_path, monkeypatch):
    monkeypatch.setenv("PUFFOAGENT_HOME", str(tmp_path))
    from puffoagent.portal.daemon import Daemon
    from puffoagent.portal.state import DaemonConfig, agent_dir, archived_dir

    # Build a fake agent dir with an archive.flag.
    agent_id = "t1"
    ad = agent_dir(agent_id)
    ad.mkdir(parents=True)
    (ad / "agent.yml").write_text("# stub\n", encoding="utf-8")
    flag = archive_flag_path(agent_id)
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text(
        json.dumps({"reason": "team_deleted", "team_id": "tid", "at": 0}),
        encoding="utf-8",
    )

    daemon = Daemon(DaemonConfig())

    _run(daemon._archive_on_flag(agent_id))

    assert not ad.exists(), "agent dir should have been moved"
    archived = list(archived_dir().glob(f"{agent_id}-ws-*"))
    assert len(archived) == 1
    moved = archived[0]
    assert (moved / "agent.yml").exists()
    # The flag travels with the dir — useful for post-hoc debugging.
    assert (moved / ".puffoagent" / "archive.flag").exists()


def test_daemon_archive_on_flag_no_agent_dir_is_noop(tmp_path, monkeypatch):
    """Racy cleanup (agent dir already gone) shouldn't crash the
    reconciler — just log and move on."""
    monkeypatch.setenv("PUFFOAGENT_HOME", str(tmp_path))
    from puffoagent.portal.daemon import Daemon
    from puffoagent.portal.state import DaemonConfig

    daemon = Daemon(DaemonConfig())
    _run(daemon._archive_on_flag("nonexistent"))
    # No raise; no dir to stat afterwards. Test passes by not raising.
