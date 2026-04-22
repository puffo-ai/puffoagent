"""Tests for the 4xx error translation in the MCP HTTP helpers.

When the agent is removed from a channel (or a channel is deleted)
mid-turn, Mattermost returns 403 or 404 on the next ``send_message``
/ ``get_channel_history`` / ``upload_file`` call. The raw body is a
terse internal id like ``api.post.create_post.post_permissions.app_error``
— fine for server logs, useless as a hint to the LLM.

``_format_http_error`` rewrites those into messages that tell the
agent:
  - what's probably going on (kicked from the channel / channel gone),
  - and explicitly to stop retrying the same call.

The signal matters because claude-code-style agents often loop on
ambiguous tool failures; a clear "do NOT retry" phrasing in the
error is the most reliable way to break the loop short of raising
exceptions the LLM can't even see.
"""

from __future__ import annotations

import pytest

from puffoagent.mcp.puffo_tools import _format_http_error


def test_403_gets_membership_hint():
    msg = _format_http_error(
        "POST", "/api/v4/posts", 403,
        '{"id":"api.post.create_post.post_permissions.app_error"}',
    )
    # Surfaces the status so reading logs still works.
    assert "403" in msg
    # Translates into a human signal the LLM will act on.
    assert "lost membership" in msg.lower() or "removed" in msg.lower()
    # Explicit don't-retry instruction.
    assert "do not retry" in msg.lower() or "do NOT retry" in msg


def test_404_gets_not_found_hint():
    msg = _format_http_error(
        "GET", "/api/v4/channels/xyz/posts", 404,
        '{"id":"store.sql_channel.get.existing.app_error"}',
    )
    assert "404" in msg
    assert "deleted" in msg.lower() or "not found" in msg.lower()
    assert "do not retry" in msg.lower() or "do NOT retry" in msg


def test_5xx_stays_generic_so_retries_are_fine():
    """5xx errors are transient — the LLM should feel free to retry.
    The phrasing must NOT tell it to stop."""
    msg = _format_http_error("POST", "/api/v4/posts", 500, "internal")
    assert "500" in msg
    assert "do not retry" not in msg.lower()


def test_401_stays_generic():
    """401 is typically an auth-layer issue handled elsewhere (refresh
    ping). The HTTP helper shouldn't bake in a channel-membership hint
    here."""
    msg = _format_http_error("GET", "/api/v4/users/me", 401, "unauthorized")
    assert "401" in msg
    assert "lost membership" not in msg.lower()


def test_body_truncated_to_200_chars():
    """Long error bodies must not balloon the tool result."""
    big = "x" * 5000
    msg = _format_http_error("GET", "/api/v4/anything", 403, big)
    # 200 is the truncation budget for the body segment; the full
    # message is longer because of the template prose.
    assert big not in msg
    assert "x" * 200 in msg


def test_method_and_path_preserved():
    """So the log still lets an operator find the exact call."""
    msg = _format_http_error("POST", "/api/v4/channels/abc/posts", 403, "")
    assert "POST" in msg
    assert "/api/v4/channels/abc/posts" in msg
