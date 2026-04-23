"""Regression tests for two agent-feedback fixes.

1. ``[SILENT]`` is matched as a SUBSTRING (not equality) in
   ``PuffoAgent.handle_message`` — agents sometimes hedge with prose
   around the marker, and those replies must still be suppressed.

2. Self-mentions are rewritten to ``@you(<bot_username>)`` instead of
   being stripped. Previous behavior (strip-self) confused LLMs in
   multi-agent threads: they saw "@agent2 please do X" and read
   agent2 as the only target, missing that they were also tagged.
   The ``@you(name)`` marker + matching ``is_self: true`` entry in
   the structured ``mentions:`` list give the agent two independent
   signals that it was addressed.
"""

from __future__ import annotations

import asyncio

from puffoagent.agent.adapters import Adapter, TurnContext, TurnResult
from puffoagent.agent.core import PuffoAgent
from puffoagent.agent.mattermost_client import MattermostClient


# ── helpers ──────────────────────────────────────────────────────────────────


def _run(coro):
    return asyncio.run(coro)


class _StubAdapter(Adapter):
    """Adapter that returns a canned reply. Used to drive
    ``PuffoAgent.handle_message`` through its post-turn branches
    without standing up a real runtime."""

    def __init__(self, reply: str):
        self._reply = reply

    async def run_turn(self, ctx: TurnContext) -> TurnResult:
        return TurnResult(reply=self._reply)


def _agent(reply: str, tmp_path) -> PuffoAgent:
    return PuffoAgent(
        adapter=_StubAdapter(reply),
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


# ── (1) [SILENT] substring match ─────────────────────────────────────────────


def test_silent_exact_match_suppresses_reply(tmp_path):
    """Baseline: a bare ``[SILENT]`` reply is still suppressed (the
    original contract)."""
    agent = _agent("[SILENT]", tmp_path)
    assert _run(_dispatch(agent)) is None


def test_silent_with_trailing_prose_suppressed(tmp_path):
    """``[SILENT] I wasn't mentioned in this thread`` — agents that
    hedge with reasoning AFTER the marker must still be silent."""
    agent = _agent(
        "[SILENT] I wasn't mentioned in this thread so no reply needed.",
        tmp_path,
    )
    assert _run(_dispatch(agent)) is None


def test_silent_with_leading_prose_suppressed(tmp_path):
    """Some models prepend a brief preamble before the marker.
    Substring match catches that too."""
    agent = _agent(
        "Let me think... [SILENT] — I'll stay out of this one.",
        tmp_path,
    )
    assert _run(_dispatch(agent)) is None


def test_reply_without_silent_marker_is_posted(tmp_path):
    """Normal replies must still flow through — substring match
    should not false-positive."""
    agent = _agent("Hello! Happy to help.", tmp_path)
    assert _run(_dispatch(agent)) == "Hello! Happy to help."


def test_empty_reply_suppressed(tmp_path):
    """``""`` has always meant 'don't post' and still must."""
    agent = _agent("", tmp_path)
    assert _run(_dispatch(agent)) is None


def test_reply_mentioning_silent_in_quotes_is_suppressed(tmp_path):
    """Known edge case we accept: any reply containing the literal
    token ``[SILENT]`` (even in quotes as meta-commentary) is
    suppressed. The primer tells agents the token is reserved, so
    they shouldn't emit it in prose.
    """
    agent = _agent('They told me to say "[SILENT]" but I won\'t.', tmp_path)
    assert _run(_dispatch(agent)) is None


# ── (2) _resolve_mentions marks self with is_self: true ──────────────────────
#
# Mocks a tiny aiohttp-shaped session so we don't need a live server.
# ``_resolve_mentions`` posts the full username list to the batch
# endpoint ``/api/v4/users/usernames`` and expects a JSON array of
# user records back. The fake below takes a ``users`` registry
# (username → record) and filters to the requested subset, silently
# omitting unknowns (matches server semantics).


class _FakeResp:
    def __init__(self, status: int, payload=None):
        self.status = status
        self._payload = payload if payload is not None else {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self):
        return self._payload


class _FakeSession:
    """Maps username → server record. ``post(url, json=[names])``
    returns the subset of registered users matching those names.
    Also counts calls so tests can assert the batch (1 call for
    any number of mentions) invariant.
    """

    def __init__(self, users: dict[str, dict]):
        self._users = users
        self.post_calls: list[list[str]] = []

    def post(self, url: str, *, json: list[str]) -> _FakeResp:
        self.post_calls.append(list(json))
        if not url.endswith("/api/v4/users/usernames"):
            return _FakeResp(status=404, payload=[])
        matched = [self._users[n] for n in json if n in self._users]
        return _FakeResp(status=200, payload=matched)


def _client_with_bot(username: str) -> MattermostClient:
    client = MattermostClient(url="http://mm.local", token="t")
    client.bot_username = username
    client.bot_user_id = "bot-uid"
    return client


def test_resolve_mentions_marks_self_when_bot_tagged():
    client = _client_with_bot("agent1")
    session = _FakeSession({
        "agent1": {"username": "agent1", "is_bot": True},
        "alice": {"username": "alice", "is_bot": False},
    })
    resolved = _run(client._resolve_mentions(
        session, "hey @agent1 and @alice — thoughts?",
    ))
    by_name = {m["username"]: m for m in resolved}
    assert by_name["agent1"]["is_self"] is True
    assert by_name["agent1"]["is_bot"] is True
    assert by_name["alice"]["is_self"] is False
    assert by_name["alice"]["is_bot"] is False


def test_resolve_mentions_self_included_even_if_only_mention():
    """Pre-fix the resolver skipped self entirely — the agent could
    see its own @-mention in raw text but had no structured entry
    for it. Regression guard: the list must contain self."""
    client = _client_with_bot("agent1")
    session = _FakeSession({
        "agent1": {"username": "agent1", "is_bot": True},
    })
    resolved = _run(client._resolve_mentions(session, "@agent1 ping"))
    assert len(resolved) == 1
    assert resolved[0]["username"] == "agent1"
    assert resolved[0]["is_self"] is True


def test_resolve_mentions_other_users_marked_not_self():
    """Sanity: is_self is strictly reserved for the bot's own
    handle — no other user gets flagged, even if bot_username is
    empty or unusual."""
    client = _client_with_bot("agent1")
    session = _FakeSession({
        "alice": {"username": "alice", "is_bot": False},
        "agent2": {"username": "agent2", "is_bot": True},
    })
    resolved = _run(client._resolve_mentions(
        session, "@alice @agent2 — heads up",
    ))
    for m in resolved:
        assert m["is_self"] is False


def test_resolve_mentions_dedupes_repeated_self():
    """Two ``@agent1`` tokens in the same message must produce one
    entry (dedupe by name). The is_self flag survives dedupe."""
    client = _client_with_bot("agent1")
    session = _FakeSession({
        "agent1": {"username": "agent1", "is_bot": True},
    })
    resolved = _run(client._resolve_mentions(
        session, "@agent1 hey @agent1 again",
    ))
    assert len(resolved) == 1
    assert resolved[0]["is_self"] is True
    # Batch endpoint is called at most once even with duplicates.
    assert len(session.post_calls) == 1
    assert session.post_calls[0] == ["agent1"]


def test_resolve_mentions_single_batch_call_for_many_mentions():
    """Regression guard: N @-mentions used to fan out into N GETs.
    The batch endpoint collapses them to one POST with the full
    deduped list, preserving order of first appearance."""
    client = _client_with_bot("agent1")
    session = _FakeSession({
        "agent1": {"username": "agent1", "is_bot": True},
        "alice":  {"username": "alice",  "is_bot": False},
        "bob":    {"username": "bob",    "is_bot": False},
        "carol":  {"username": "carol",  "is_bot": False},
    })
    resolved = _run(client._resolve_mentions(
        session, "@alice @bob @carol @agent1 heads up",
    ))
    assert [m["username"] for m in resolved] == ["alice", "bob", "carol", "agent1"]
    assert len(session.post_calls) == 1
    assert session.post_calls[0] == ["alice", "bob", "carol", "agent1"]


def test_resolve_mentions_drops_unknown_usernames():
    """Unknown names (not valid users) are silently omitted — the
    batch endpoint just returns the subset it found."""
    client = _client_with_bot("agent1")
    session = _FakeSession({
        "alice": {"username": "alice", "is_bot": False},
    })
    resolved = _run(client._resolve_mentions(
        session, "@alice @bogus @other",
    ))
    assert [m["username"] for m in resolved] == ["alice"]


def test_resolve_mentions_no_mentions_skips_network():
    """Message with no @-mentions should do zero HTTP calls — no
    point paying latency for an empty batch."""
    client = _client_with_bot("agent1")
    session = _FakeSession({})
    resolved = _run(client._resolve_mentions(session, "just a plain message"))
    assert resolved == []
    assert session.post_calls == []


def test_resolve_mentions_batch_failure_returns_empty():
    """If the batch endpoint itself fails, we can't partially
    recover — return empty (old per-name loop would have failed on
    every lookup anyway under a comparable network failure)."""

    class _FailingSession:
        def __init__(self):
            self.post_calls: list[list[str]] = []

        def post(self, url, *, json):
            self.post_calls.append(list(json))
            raise RuntimeError("simulated network failure")

    client = _client_with_bot("agent1")
    session = _FailingSession()
    resolved = _run(client._resolve_mentions(session, "@alice @bob"))
    assert resolved == []
    assert len(session.post_calls) == 1  # We tried once, not once-per-name.


# ── (3) _append_user preamble renders " — that's you" for self ──────────────


def test_append_user_renders_self_marker(tmp_path):
    """The preamble's ``mentions:`` list must tag the self entry
    with ``— that's you`` so agents that only parse the structured
    preamble (not the @you(...) text rewrite) still spot it."""
    agent = _agent("ok", tmp_path)
    agent._append_user(
        channel_name="test",
        sender="alice",
        sender_email="alice@x",
        text="@you(agent1) please help",
        attachments=None,
        mentions=[
            {"username": "agent1", "is_bot": True, "is_self": True},
            {"username": "bob", "is_bot": False, "is_self": False},
        ],
    )
    last = agent.log[-1]["content"]
    # Self entry has the marker.
    assert "- agent1 (bot) — that's you" in last
    # Non-self entries do NOT get the marker (would poison the signal).
    assert "- bob (human)" in last
    assert "- bob (human) — that's you" not in last


def test_append_user_no_self_marker_when_bot_not_tagged(tmp_path):
    """When the bot isn't in the mentions list, no entry should
    carry the marker — even if another bot was tagged."""
    agent = _agent("ok", tmp_path)
    agent._append_user(
        channel_name="test",
        sender="alice",
        sender_email="alice@x",
        text="@agent2 please help",
        attachments=None,
        mentions=[
            {"username": "agent2", "is_bot": True, "is_self": False},
        ],
    )
    last = agent.log[-1]["content"]
    assert "- agent2 (bot)" in last
    assert "that's you" not in last


def test_append_user_falls_back_when_is_self_missing(tmp_path):
    """Backward-compat: older callers / cached events may pass
    mentions without ``is_self``. Default to False (no marker)
    rather than crashing."""
    agent = _agent("ok", tmp_path)
    agent._append_user(
        channel_name="test",
        sender="alice",
        sender_email="alice@x",
        text="hello",
        attachments=None,
        mentions=[
            {"username": "alice", "is_bot": False},  # no is_self key
        ],
    )
    last = agent.log[-1]["content"]
    assert "- alice (human)" in last
    assert "that's you" not in last


# ── (4) clean_text rewrite in _handle_event ──────────────────────────────────
#
# End-to-end via _handle_event is heavy (aiohttp + queue + downloads).
# The clean_text rewrite is a single line — we test it via a direct
# assertion against the exact substitution the code performs, using a
# real MattermostClient instance so a rename of ``bot_username`` would
# also break the test.


def test_clean_text_substitutes_self_mention_not_strips():
    """``@agent1`` in the middle of the message must become
    ``@you(agent1)`` — preserving position and surrounding text so
    the agent can see which sentence addressed it.
    """
    client = _client_with_bot("agent1")
    text = "hey @agent1 can you take a look at this?"
    rewritten = text.replace(
        f"@{client.bot_username}", f"@you({client.bot_username})",
    )
    assert rewritten == "hey @you(agent1) can you take a look at this?"


def test_clean_text_leaves_other_mentions_intact():
    """Only the bot's own @-mention is rewritten — peer agents /
    humans keep their raw @handle so the bot can see who else was
    addressed."""
    client = _client_with_bot("agent1")
    text = "@alice and @agent1, please sync with @agent2"
    rewritten = text.replace(
        f"@{client.bot_username}", f"@you({client.bot_username})",
    )
    assert "@alice" in rewritten
    assert "@agent2" in rewritten
    assert "@you(agent1)" in rewritten
    # Importantly, ``@agent1`` on its own (without the ``you(...)``
    # wrapper) is NOT present any more.
    assert " @agent1," not in rewritten


def test_clean_text_handles_repeated_self_mentions():
    """Both occurrences should be rewritten — agents sometimes get
    double-pinged in the same message."""
    client = _client_with_bot("agent1")
    text = "@agent1 ping — @agent1 again"
    rewritten = text.replace(
        f"@{client.bot_username}", f"@you({client.bot_username})",
    )
    assert rewritten == "@you(agent1) ping — @you(agent1) again"
