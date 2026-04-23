"""Regression guard: when the agent calls ``mcp__puffo__send_message``,
the MCP server has already posted to the channel. Any narration text
captured in the same turn must NOT be auto-posted by the shell — that
was the ``double-post`` bug.

The contract we verify here (in `PuffoAgent.handle_message`):

  * metadata["tool_names"] contains "mcp__puffo__send_message"  ->
      handle_message returns None (no auto-reply), but the reply
      text IS appended to ``agent.log`` so future turns still see
      it as context.

  * metadata["tool_names"] without send_message -> behaves as
      before: non-[SILENT] replies get posted.

  * metadata missing entirely or empty (legacy adapters, ChatOnly)
      -> behaves as before (no suppression).

The adapter side of the contract — that cli_session and sdk actually
populate ``tool_names`` — is exercised in their respective adapter
tests; this file is strictly about the shell's decision logic.
"""

from __future__ import annotations

import asyncio

from puffoagent.agent.adapters import Adapter, TurnContext, TurnResult
from puffoagent.agent.core import PuffoAgent


# ── helpers ──────────────────────────────────────────────────────────────────


def _run(coro):
    return asyncio.run(coro)


class _StubAdapter(Adapter):
    """Adapter that returns a canned ``TurnResult`` including
    metadata. Lets us drive ``handle_message`` through the
    suppress-after-send_message branch deterministically."""

    def __init__(self, reply: str, tool_names: list[str] | None = None):
        self._reply = reply
        self._tool_names = tool_names

    async def run_turn(self, ctx: TurnContext) -> TurnResult:
        meta: dict = {}
        if self._tool_names is not None:
            meta["tool_names"] = list(self._tool_names)
        return TurnResult(reply=self._reply, metadata=meta)


def _agent(reply: str, tmp_path, tool_names: list[str] | None = None) -> PuffoAgent:
    return PuffoAgent(
        adapter=_StubAdapter(reply, tool_names),
        system_prompt="you are a test bot",
        memory_dir=str(tmp_path),
    )


async def _dispatch(agent: PuffoAgent, text: str = "hi") -> str | None:
    return await agent.handle_message(
        channel_id="c1",
        channel_name="test",
        sender="u",
        sender_email="u@x",
        text=text,
    )


def _assistant_entries(agent: PuffoAgent) -> list[dict]:
    return [entry for entry in agent.log if entry.get("role") == "assistant"]


# ── suppression when send_message was called ─────────────────────────────────


def test_send_message_suppresses_autoreply(tmp_path):
    """The canonical double-post scenario: agent narrates
    ("Replied in thread.") while also invoking send_message.
    handle_message must return None."""
    agent = _agent(
        "Replied in thread.",
        tmp_path,
        tool_names=["mcp__puffo__send_message"],
    )
    assert _run(_dispatch(agent)) is None


def test_send_message_suppress_still_logs_assistant(tmp_path):
    """Even when suppressing the auto-reply, the reply text must be
    appended to agent.log so the next turn sees the narration as
    context. Without this, the agent loses continuity when it uses
    send_message mid-conversation."""
    agent = _agent(
        "Replied in thread.",
        tmp_path,
        tool_names=["mcp__puffo__send_message"],
    )
    _run(_dispatch(agent))
    assistants = _assistant_entries(agent)
    assert len(assistants) == 1
    assert "Replied in thread." in assistants[0]["content"]


def test_send_message_alongside_other_tools_still_suppresses(tmp_path):
    """A mixed turn (Read + Bash + send_message) still counts as
    'agent already posted' — any send_message call triggers the
    suppression regardless of how many other tools were used."""
    agent = _agent(
        "Let me read the file... done.",
        tmp_path,
        tool_names=["Read", "Bash", "mcp__puffo__send_message"],
    )
    assert _run(_dispatch(agent)) is None


# ── no suppression when send_message wasn't involved ─────────────────────────


def test_plain_reply_without_tools_still_posts(tmp_path):
    """Empty tool_names list (agent answered purely from knowledge)
    must behave like the pre-fix path: reply gets posted."""
    agent = _agent("Hello!", tmp_path, tool_names=[])
    assert _run(_dispatch(agent)) == "Hello!"


def test_reply_with_other_tools_but_no_send_message_still_posts(tmp_path):
    """Agent used Read + Bash but did NOT call send_message — the
    reply is the intended channel response and must be posted."""
    agent = _agent(
        "Found it: 42.",
        tmp_path,
        tool_names=["Read", "Bash"],
    )
    assert _run(_dispatch(agent)) == "Found it: 42."


def test_metadata_without_tool_names_key_still_posts(tmp_path):
    """Legacy adapters / ChatOnlyAdapter return no metadata — the
    suppression check must fall through safely to the default
    'post it' behaviour."""
    agent = _agent("Hello!", tmp_path, tool_names=None)  # no metadata at all
    assert _run(_dispatch(agent)) == "Hello!"


# ── interaction with the [SILENT] check ──────────────────────────────────────


def test_silent_still_suppresses_even_if_send_message_present(tmp_path):
    """If the agent emits [SILENT] AND called send_message, the
    [SILENT] branch wins (returns None before the send_message
    branch is reached). Behaviour identical to today — this just
    guards against a future reordering regressing it."""
    agent = _agent(
        "[SILENT]",
        tmp_path,
        tool_names=["mcp__puffo__send_message"],
    )
    assert _run(_dispatch(agent)) is None


def test_silent_does_not_append_assistant_log_entry(tmp_path):
    """Paired with the above: the [SILENT] branch returns None
    WITHOUT appending to agent.log. The send_message branch DOES
    append. This test documents the asymmetry (the [SILENT]
    contract has always been 'pretend we never spoke')."""
    agent = _agent(
        "[SILENT]",
        tmp_path,
        tool_names=["mcp__puffo__send_message"],
    )
    _run(_dispatch(agent))
    assert _assistant_entries(agent) == []
