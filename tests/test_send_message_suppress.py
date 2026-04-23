"""Regression + precision tests for the ``send_message`` double-post guard.

The worker posts the shell's auto-reply to ``(channel_id, root_id)`` —
the same conversation slot as the incoming message (see
``portal/worker.py`` where ``client.post_message(channel_id, reply,
root_id=root_id)`` is called after ``handle_message`` returns).

``PuffoAgent.handle_message`` suppresses its auto-reply IFF at least
one ``send_message`` call this turn landed in that exact slot:

  * channel match: the tool's ``channel`` arg equals either the
    incoming message's ``channel_id`` OR its ``channel_name`` (the
    MCP tool accepts either).

  * thread match: the tool's ``root_id`` equals the incoming
    ``root_id`` (string equality; empty means "top-level in
    channel", which still counts).

Any suppressed reply is still appended to ``agent.log`` so the next
turn sees it as context — only the outbound post is skipped.

What these tests do NOT cover: the adapter-side plumbing that
populates ``send_message_targets`` from CLI/SDK tool_use events.
That's exercised in the adapter suites (cli_session_recovery
etc. via ``TurnResult.metadata``).
"""

from __future__ import annotations

import asyncio

from puffoagent.agent.adapters import Adapter, TurnContext, TurnResult
from puffoagent.agent.core import PuffoAgent


# ── helpers ──────────────────────────────────────────────────────────────────


def _run(coro):
    return asyncio.run(coro)


class _StubAdapter(Adapter):
    """Returns a canned ``TurnResult`` with the metadata we want
    the shell to see. Lets every test drive ``handle_message``
    through a specific metadata shape deterministically."""

    def __init__(
        self,
        reply: str,
        *,
        tool_names: list[str] | None = None,
        send_message_targets: list[dict] | None = None,
    ):
        self._reply = reply
        self._tool_names = tool_names
        self._targets = send_message_targets

    async def run_turn(self, ctx: TurnContext) -> TurnResult:
        meta: dict = {}
        if self._tool_names is not None:
            meta["tool_names"] = list(self._tool_names)
        if self._targets is not None:
            meta["send_message_targets"] = [dict(t) for t in self._targets]
        return TurnResult(reply=self._reply, metadata=meta)


def _agent(reply: str, tmp_path, **meta) -> PuffoAgent:
    return PuffoAgent(
        adapter=_StubAdapter(reply, **meta),
        system_prompt="you are a test bot",
        memory_dir=str(tmp_path),
    )


async def _dispatch(
    agent: PuffoAgent,
    *,
    channel_id: str = "c-main",
    channel_name: str = "general",
    root_id: str = "",
    post_id: str = "p-1",
    text: str = "hi",
) -> str | None:
    return await agent.handle_message(
        channel_id=channel_id,
        channel_name=channel_name,
        sender="u",
        sender_email="u@x",
        text=text,
        post_id=post_id,
        root_id=root_id,
    )


def _assistant_entries(agent: PuffoAgent) -> list[dict]:
    return [e for e in agent.log if e.get("role") == "assistant"]


# ── suppress: send_message landed in the current slot ───────────────────────


def test_suppresses_when_send_message_matches_channel_id_top_level(tmp_path):
    """Incoming is a top-level post; agent send_message's to the
    same channel_id with empty root_id. Both land as top-level
    posts in the same channel — the user would see two messages."""
    agent = _agent(
        "Replied in thread.",
        tmp_path,
        send_message_targets=[{"channel": "c-main", "root_id": ""}],
    )
    result = _run(_dispatch(agent, channel_id="c-main", channel_name="general", root_id=""))
    assert result is None


def test_suppresses_when_send_message_matches_channel_name(tmp_path):
    """The tool accepts a name OR id. ``general`` matches the
    channel_name, which is enough for the channel half of the
    match."""
    agent = _agent(
        "done",
        tmp_path,
        send_message_targets=[{"channel": "general", "root_id": ""}],
    )
    result = _run(_dispatch(agent, channel_id="c-main", channel_name="general", root_id=""))
    assert result is None


def test_suppresses_when_send_message_matches_thread(tmp_path):
    """Incoming is a threaded reply; agent send_message's to the
    same thread root — same slot, suppress."""
    agent = _agent(
        "ack",
        tmp_path,
        send_message_targets=[{"channel": "c-main", "root_id": "thread-abc"}],
    )
    result = _run(_dispatch(
        agent, channel_id="c-main", channel_name="general", root_id="thread-abc"
    ))
    assert result is None


def test_suppression_still_appends_to_agent_log(tmp_path):
    """The suppressed reply must still land in ``agent.log`` so the
    next turn sees it as context. Only the outbound post is skipped."""
    agent = _agent(
        "Replied in thread.",
        tmp_path,
        send_message_targets=[{"channel": "c-main", "root_id": ""}],
    )
    _run(_dispatch(agent, channel_id="c-main", channel_name="general", root_id=""))
    assistants = _assistant_entries(agent)
    assert len(assistants) == 1
    assert "Replied in thread." in assistants[0]["content"]


def test_suppresses_when_multiple_targets_include_current_slot(tmp_path):
    """Agent fanned out — one send_message to another channel AND
    one to the current slot. The current-slot one is enough to
    trigger suppression."""
    agent = _agent(
        "broadcasting",
        tmp_path,
        send_message_targets=[
            {"channel": "other-channel", "root_id": ""},
            {"channel": "c-main", "root_id": ""},
        ],
    )
    result = _run(_dispatch(agent, channel_id="c-main", channel_name="general", root_id=""))
    assert result is None


# ── do NOT suppress: send_message landed elsewhere ──────────────────────────


def test_does_not_suppress_when_send_message_targets_different_channel(tmp_path):
    """Agent fanned out to ``#ops`` while narrating in the current
    channel. Auto-reply in the current channel is legitimate —
    must post."""
    agent = _agent(
        "FYI, pinged #ops.",
        tmp_path,
        send_message_targets=[{"channel": "ops", "root_id": ""}],
    )
    result = _run(_dispatch(agent, channel_id="c-main", channel_name="general", root_id=""))
    assert result == "FYI, pinged #ops."


def test_does_not_suppress_when_send_message_targets_different_thread(tmp_path):
    """Same channel but a different thread root. Auto-reply lands
    in the current thread; the send_message is in a sibling thread.
    Distinct conversations — must post."""
    agent = _agent(
        "still here.",
        tmp_path,
        send_message_targets=[{"channel": "c-main", "root_id": "thread-xyz"}],
    )
    result = _run(_dispatch(
        agent, channel_id="c-main", channel_name="general", root_id="thread-abc"
    ))
    assert result == "still here."


def test_does_not_suppress_top_level_send_message_when_incoming_is_threaded(tmp_path):
    """send_message with empty root_id creates a new top-level post;
    incoming is threaded. Auto-reply goes to the thread (empty
    root_id wouldn't match), so NO duplicate — must post."""
    agent = _agent(
        "replying in thread.",
        tmp_path,
        send_message_targets=[{"channel": "c-main", "root_id": ""}],
    )
    result = _run(_dispatch(
        agent, channel_id="c-main", channel_name="general", root_id="thread-abc"
    ))
    assert result == "replying in thread."


def test_does_not_suppress_threaded_send_message_when_incoming_is_top_level(tmp_path):
    """Inverse: agent replied in a thread via send_message, but
    incoming (and so the auto-reply) is top-level in the channel.
    Different slots — must post."""
    agent = _agent(
        "top-level reply.",
        tmp_path,
        send_message_targets=[{"channel": "c-main", "root_id": "thread-xyz"}],
    )
    result = _run(_dispatch(agent, channel_id="c-main", channel_name="general", root_id=""))
    assert result == "top-level reply."


def test_does_not_suppress_when_no_send_message_called(tmp_path):
    """Agent used other tools (Read, Bash) but never send_message.
    Must post — this is the path for the 'normal' narrative-reply
    flow."""
    agent = _agent(
        "42",
        tmp_path,
        tool_names=["Read", "Bash"],
        send_message_targets=[],
    )
    result = _run(_dispatch(agent))
    assert result == "42"


def test_does_not_suppress_when_metadata_missing(tmp_path):
    """Legacy adapters (e.g. ChatOnlyAdapter) don't populate
    metadata at all. Fall through to the post-it path unchanged."""
    agent = _agent("Hello!", tmp_path)  # no metadata kwargs
    result = _run(_dispatch(agent))
    assert result == "Hello!"


# ── edge cases ──────────────────────────────────────────────────────────────


def test_empty_channel_target_is_ignored(tmp_path):
    """If ``send_message`` was somehow invoked with an empty
    channel string (shouldn't happen, but defensive) it can't
    match anything — must not crash, must not false-suppress."""
    agent = _agent(
        "ok",
        tmp_path,
        send_message_targets=[{"channel": "", "root_id": ""}],
    )
    result = _run(_dispatch(agent, channel_id="c-main", channel_name="general", root_id=""))
    assert result == "ok"


def test_missing_root_id_in_target_treats_as_top_level(tmp_path):
    """A send_message target dict without a ``root_id`` key must
    be treated as empty (top-level). With empty incoming root_id,
    that's a match."""
    agent = _agent(
        "ok",
        tmp_path,
        send_message_targets=[{"channel": "c-main"}],  # no root_id key
    )
    result = _run(_dispatch(agent, channel_id="c-main", channel_name="general", root_id=""))
    assert result is None


# ── interaction with the [SILENT] check ─────────────────────────────────────


def test_silent_wins_over_send_message_suppression(tmp_path):
    """[SILENT] branch runs first and returns None without touching
    agent.log. If send_message was ALSO called, the [SILENT] path's
    'no log append' behaviour is preserved — documented asymmetry."""
    agent = _agent(
        "[SILENT]",
        tmp_path,
        send_message_targets=[{"channel": "c-main", "root_id": ""}],
    )
    assert _run(_dispatch(agent, channel_id="c-main", channel_name="general", root_id="")) is None
    # [SILENT] contract: no assistant entry in the log.
    assert _assistant_entries(agent) == []
