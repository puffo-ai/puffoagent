"""Tests for the harness abstraction layer.

Covers three things:

  1. ``build_harness`` resolves agent.yml's ``runtime.harness`` string
     into a concrete ``Harness`` instance. Default + explicit values.
  2. ``supports_claude_specific_tools()`` lines up with the MCP tool
     gating: claude-code → True (existing behavior); hermes → False
     (new).
  3. The MCP tool guard (``_require_claude_code``) raises with a
     clear message under a non-claude harness, so agents stop
     retrying tools that wouldn't take effect.

We don't test the actual `hermes chat -q` subprocess here; that
requires a live hermes install and the Anthropic API — exercised via
the d2d2 smoke test during the rollout, not unit tests.
"""

from __future__ import annotations

import asyncio

import pytest

from puffoagent.agent.harness import (
    ClaudeCodeHarness,
    Harness,
    HermesHarness,
    build_harness,
)


# ── build_harness ────────────────────────────────────────────────────────────


def test_build_harness_defaults_to_claude_code():
    """Existing agents without the field set must behave as before.
    This is the backward-compat contract — any agent on disk before
    v0.6.0 has ``harness=""`` in its loaded config."""
    h = build_harness("")
    assert isinstance(h, ClaudeCodeHarness)
    assert h.name() == "claude-code"


def test_build_harness_explicit_claude_code():
    h = build_harness("claude-code")
    assert isinstance(h, ClaudeCodeHarness)


def test_build_harness_hermes():
    h = build_harness("hermes")
    assert isinstance(h, HermesHarness)
    assert h.name() == "hermes"


def test_build_harness_unknown_raises():
    with pytest.raises(ValueError, match="unknown harness"):
        build_harness("not-a-harness")


# ── supports_claude_specific_tools ────────────────────────────────────────────


def test_claude_code_supports_claude_tools():
    assert ClaudeCodeHarness().supports_claude_specific_tools() is True


def test_hermes_does_not_support_claude_tools():
    """The Claude-specific MCP tools (install_skill, refresh, etc.)
    write to paths Hermes doesn't read. This flag is what gates them."""
    assert HermesHarness().supports_claude_specific_tools() is False


def test_base_harness_defaults_to_not_supporting():
    """New harness authors must opt IN to claude-specific tools, not
    OUT. This way a forgotten override can't silently enable write
    paths a harness doesn't understand."""
    class MinimalHarness(Harness):
        def name(self) -> str:
            return "minimal"
    assert MinimalHarness().supports_claude_specific_tools() is False


# ── _require_claude_code guard ───────────────────────────────────────────────
#
# The guard lives inside ``build_server`` as a closure over cfg, so
# exercising it directly means standing up the FastMCP server. Simpler
# path: reach into the tool registry.


def _run(coro):
    return asyncio.run(coro)


def _build_mcp_with_harness(harness: str):
    from puffoagent.mcp.puffo_tools import ToolsConfig, build_server
    cfg = ToolsConfig(
        agent_id="t",
        url="http://localhost:8065",
        token="bot-token",
        workspace="/tmp/ws",
        harness=harness,
    )
    return build_server(cfg), cfg


def _call_tool(server, tool_name, **kwargs):
    """Invoke a registered tool by name. FastMCP exposes them via the
    tool manager; call the underlying fn directly so we bypass the
    stdio protocol plumbing."""
    tool = server._tool_manager._tools[tool_name]
    # FastMCP stores the original async function on the tool object.
    return _run(tool.fn(**kwargs))


def test_install_skill_blocked_under_hermes(tmp_path):
    server, _ = _build_mcp_with_harness("hermes")
    with pytest.raises(RuntimeError, match="only supported under the claude-code harness"):
        _call_tool(server, "install_skill", name="my-skill", content="body")


def test_install_skill_allowed_under_claude_code(tmp_path, monkeypatch):
    """Sanity check: the guard doesn't false-positive on the normal
    case."""
    monkeypatch.chdir(tmp_path)
    server, cfg = _build_mcp_with_harness("claude-code")
    # Override workspace to tmp_path so the write lands under pytest's
    # managed dir.
    cfg.workspace = str(tmp_path)
    # Re-build to pick up the new workspace in the closure.
    server, _ = _build_mcp_with_harness("claude-code")
    # The tool writes inside cfg.workspace. Use the rebuilt server's cfg path.
    # (Simpler: just confirm no RuntimeError about harness — let the
    # actual skill write either succeed or fail on tmpdir specifics,
    # not on harness gating.)
    try:
        _call_tool(
            server, "install_skill",
            name="ok", content="# valid skill body",
        )
    except RuntimeError as exc:
        assert "claude-code harness" not in str(exc), (
            "claude-code harness should NOT be blocked by _require_claude_code"
        )


def test_refresh_blocked_under_hermes():
    server, _ = _build_mcp_with_harness("hermes")
    with pytest.raises(RuntimeError, match="only supported under the claude-code harness"):
        _call_tool(server, "refresh")


def test_install_mcp_server_blocked_under_hermes():
    server, _ = _build_mcp_with_harness("hermes")
    with pytest.raises(RuntimeError, match="only supported under the claude-code harness"):
        _call_tool(
            server, "install_mcp_server",
            name="test", command="npx", args=["-y", "@foo/bar"], env={},
        )


def test_uninstall_tools_blocked_under_hermes():
    server, _ = _build_mcp_with_harness("hermes")
    with pytest.raises(RuntimeError, match="only supported under the claude-code harness"):
        _call_tool(server, "uninstall_skill", name="x")
    with pytest.raises(RuntimeError, match="only supported under the claude-code harness"):
        _call_tool(server, "uninstall_mcp_server", name="x")


def test_list_tools_not_blocked_under_hermes():
    """list_skills / list_mcp_servers are READ-only and useful for
    any harness to introspect what's on disk — no guard."""
    server, _ = _build_mcp_with_harness("hermes")
    # Should NOT raise; returning empty / not-installed is fine.
    result = _call_tool(server, "list_skills")
    assert isinstance(result, str)
    result = _call_tool(server, "list_mcp_servers")
    assert isinstance(result, str)


def test_harness_empty_means_backward_compat_not_blocked():
    """Old daemons that don't set PUFFO_HARNESS at all must still let
    claude-specific tools through — otherwise an upgrade breaks
    agents mid-turn."""
    server, _ = _build_mcp_with_harness("")
    # Should NOT raise a harness error. The refresh flag will fail
    # elsewhere (workspace doesn't exist), but that's not what we're
    # testing.
    try:
        _call_tool(server, "refresh")
    except RuntimeError as exc:
        assert "only supported under" not in str(exc), (
            f"empty harness should not trigger the guard: {exc}"
        )
