"""Unit tests for the cli-local permission proxy.

Covers the two pieces that most often break in silence:

  - ``_resolve_owner_dm`` refuses early when owner_username is
    unset (this used to fail deep inside the poll loop, looking
    like a hang).
  - ``_await_permission_reply`` correctly maps owner replies to
    allow / deny / timeout and ignores irrelevant posts.

The ``approve_permission`` tool itself is tested via a fake session
that drives the whole flow without a live Mattermost.
"""

import asyncio
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from puffoagent.mcp.puffo_tools import (
    ToolsConfig,
    _await_permission_reply,
    _resolve_owner_dm,
)


# ── Fake aiohttp session ──────────────────────────────────────────────────────


class _FakeCM:
    """Async context manager that yields a fixed response-like obj."""
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *a):
        return False


class _FakeResp:
    def __init__(self, status: int, body: Any):
        self.status = status
        self._body = body

    async def json(self):
        return self._body

    async def text(self):
        import json
        return json.dumps(self._body) if not isinstance(self._body, str) else self._body


class _FakeSession:
    """Drop-in stand-in for ``aiohttp.ClientSession`` that returns
    scripted responses keyed by path substring. Supports both
    ``get`` and ``post`` with the async-context-manager shape the
    real client uses.
    """
    def __init__(self, get_handler: Callable[[str], _FakeResp] = None,
                 post_handler: Callable[[str, Any], _FakeResp] = None):
        self._get = get_handler or (lambda u: _FakeResp(404, {"error": "no handler"}))
        self._post = post_handler or (lambda u, p: _FakeResp(404, {"error": "no handler"}))

    def get(self, url: str):
        return _FakeCM(self._get(url))

    def post(self, url: str, json=None):
        return _FakeCM(self._post(url, json))


def _run(coro):
    """Tiny helper so tests don't need pytest-asyncio."""
    return asyncio.run(coro)


# ── _resolve_owner_dm ─────────────────────────────────────────────────────────


class TestResolveOwnerDm:
    def test_missing_owner_username_raises(self):
        cfg = ToolsConfig(
            agent_id="a", url="http://x", token="t", workspace="/",
            owner_username="",
        )
        fake = _FakeSession()
        with pytest.raises(RuntimeError, match="owner-username"):
            _run(_resolve_owner_dm(fake, cfg, "bot-id"))

    def test_happy_path_returns_owner_and_dm_ids(self):
        cfg = ToolsConfig(
            agent_id="a", url="http://x", token="t", workspace="/",
            owner_username="han",
        )
        # GET /users/username/han → returns the user record
        # POST /channels/direct → returns the DM channel record
        def get_handler(u):
            assert u.endswith("/api/v4/users/username/han")
            return _FakeResp(200, {"id": "owner-26charid"})

        def post_handler(u, payload):
            assert u.endswith("/api/v4/channels/direct")
            assert payload == ["bot-id", "owner-26charid"]
            return _FakeResp(201, {"id": "dm-26charid"})

        fake = _FakeSession(get_handler=get_handler, post_handler=post_handler)
        owner_id, dm = _run(_resolve_owner_dm(fake, cfg, "bot-id"))
        assert owner_id == "owner-26charid"
        assert dm == "dm-26charid"


# ── _await_permission_reply ───────────────────────────────────────────────────


def _thread_response(posts: list[dict]) -> dict:
    """Build a Mattermost /posts/{root}/thread response. Same
    shape as a flat channel-posts response: {order, posts}."""
    return {
        "order": [p["id"] for p in posts],
        "posts": {p["id"]: p for p in posts},
    }


# Back-compat alias for the tests below — the shape is identical
# between channel-posts and thread responses, so just rename.
_channel_posts_response = _thread_response


class TestAwaitPermissionReply:
    def _cfg(self, timeout: float = 10.0) -> ToolsConfig:
        return ToolsConfig(
            agent_id="a", url="http://x", token="t", workspace="/",
            owner_username="han",
            permission_timeout_seconds=timeout,
        )

    def test_allow_on_y_reply(self):
        owner = "owner-id"
        now = 10_000  # epoch seconds
        def get_handler(u):
            return _FakeResp(200, _channel_posts_response([
                {"id": "p1", "user_id": owner,
                 "create_at": (now + 1) * 1000,  # ms, strictly after since_ts
                 "message": "yes please"},
            ]))
        fake = _FakeSession(get_handler=get_handler)
        decision = _run(_await_permission_reply(
            fake, self._cfg(), "thread-root-id", owner, since_ts=now,
        ))
        assert decision is True

    def test_allow_on_approve(self):
        owner = "owner-id"
        now = 10_000
        def get_handler(u):
            return _FakeResp(200, _channel_posts_response([
                {"id": "p1", "user_id": owner,
                 "create_at": (now + 1) * 1000,
                 "message": "approve"},
            ]))
        fake = _FakeSession(get_handler=get_handler)
        decision = _run(_await_permission_reply(
            fake, self._cfg(), "thread-root-id", owner, since_ts=now,
        ))
        assert decision is True

    def test_deny_on_n_reply(self):
        owner = "owner-id"
        now = 10_000
        def get_handler(u):
            return _FakeResp(200, _channel_posts_response([
                {"id": "p1", "user_id": owner,
                 "create_at": (now + 1) * 1000,
                 "message": "nope, that's dangerous"},
            ]))
        fake = _FakeSession(get_handler=get_handler)
        decision = _run(_await_permission_reply(
            fake, self._cfg(), "thread-root-id", owner, since_ts=now,
        ))
        assert decision is False

    def test_ignores_posts_from_other_users(self):
        # A chatty channel might have bot posts + other people's
        # comments. Only the owner's reply counts — but we have a
        # 10s timeout so we can't wait forever. Use a tight timeout.
        owner = "owner-id"
        bot = "bot-id"
        now = 10_000

        def get_handler(u):
            return _FakeResp(200, _channel_posts_response([
                {"id": "p2", "user_id": bot,
                 "create_at": (now + 1) * 1000,
                 "message": "yes (agent self-reply, should ignore)"},
            ]))

        fake = _FakeSession(get_handler=get_handler)
        # Use a tiny timeout — we want the assertion that None is
        # returned despite there being a "y..." message from a
        # non-owner.
        decision = _run(_await_permission_reply(
            fake, self._cfg(timeout=0.1), "thread-root-id", owner, since_ts=now,
        ))
        assert decision is None

    def test_ignores_old_posts_before_since_ts(self):
        owner = "owner-id"
        now = 10_000
        def get_handler(u):
            return _FakeResp(200, _channel_posts_response([
                # Owner replied, but BEFORE the request was posted.
                {"id": "p1", "user_id": owner,
                 "create_at": (now - 5) * 1000,
                 "message": "yes (to the previous request)"},
            ]))
        fake = _FakeSession(get_handler=get_handler)
        decision = _run(_await_permission_reply(
            fake, self._cfg(timeout=0.1), "thread-root-id", owner, since_ts=now,
        ))
        assert decision is None

    def test_timeout_returns_none(self):
        owner = "owner-id"
        now = 10_000
        # No qualifying posts — just bot noise.
        def get_handler(u):
            return _FakeResp(200, _channel_posts_response([]))
        fake = _FakeSession(get_handler=get_handler)
        decision = _run(_await_permission_reply(
            fake, self._cfg(timeout=0.1), "thread-root-id", owner, since_ts=now,
        ))
        assert decision is None

    def test_polls_thread_endpoint_not_channel(self):
        # Regression guard: before threading, we polled
        # /channels/{id}/posts which led to replies being credited
        # to the wrong in-flight request. Make sure we're now
        # hitting the /posts/{root}/thread endpoint.
        owner = "owner-id"
        now = 10_000
        seen_urls: list[str] = []

        def get_handler(u):
            seen_urls.append(u)
            return _FakeResp(200, _channel_posts_response([
                {"id": "reply1", "user_id": owner,
                 "create_at": (now + 1) * 1000,
                 "message": "y"},
            ]))

        fake = _FakeSession(get_handler=get_handler)
        _run(_await_permission_reply(
            fake, self._cfg(), "thread-root-id", owner, since_ts=now,
        ))
        assert seen_urls, "no HTTP call was made"
        assert "/api/v4/posts/thread-root-id/thread" in seen_urls[0]
        assert "/channels/" not in seen_urls[0]

    def test_http_error_keeps_polling(self):
        # First call fails (network blip), second succeeds with an
        # allow. The poll loop should survive the first failure.
        owner = "owner-id"
        now = 10_000
        calls = {"n": 0}

        def get_handler(u):
            calls["n"] += 1
            if calls["n"] == 1:
                return _FakeResp(500, "oops")
            return _FakeResp(200, _channel_posts_response([
                {"id": "p1", "user_id": owner,
                 "create_at": (now + 1) * 1000,
                 "message": "y"},
            ]))

        fake = _FakeSession(get_handler=get_handler)
        # Give enough time for at least 2 iterations (poll sleeps 2s
        # between iterations).
        decision = _run(_await_permission_reply(
            fake, self._cfg(timeout=5.0), "thread-root-id", owner, since_ts=now,
        ))
        assert decision is True
        assert calls["n"] >= 2
