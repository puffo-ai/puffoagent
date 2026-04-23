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


# ── supported_providers (runtime-matrix feed) ────────────────────────────────


def test_claude_code_providers_anthropic_only():
    assert ClaudeCodeHarness().supported_providers() == frozenset({"anthropic"})


def test_hermes_providers_anthropic_and_openai():
    assert HermesHarness().supported_providers() == frozenset({"anthropic", "openai"})


def test_gemini_cli_providers_google_only():
    from puffoagent.agent.harness import GeminiCLIHarness
    assert GeminiCLIHarness().supported_providers() == frozenset({"google"})


def test_base_harness_declares_empty_provider_set():
    """Default empty set forces concrete harnesses to opt in.
    An empty set means the validation matrix will reject every
    provider, which is the safe fallback."""
    class MinimalHarness(Harness):
        def name(self) -> str:
            return "minimal"
    assert MinimalHarness().supported_providers() == frozenset()


def test_build_harness_accepts_gemini_cli():
    from puffoagent.agent.harness import GeminiCLIHarness
    h = build_harness("gemini-cli")
    assert isinstance(h, GeminiCLIHarness)
    assert h.name() == "gemini-cli"


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


# ── Hermes subprocess helpers (parse / normalize / stitch) ───────────────────
#
# The docker adapter calls ``hermes chat --quiet -q ...`` per turn and
# parses stdout. The pure helpers below are what the parsing relies on;
# testing them without a live container ensures the shape assumptions
# are explicit and easy to revisit if hermes changes its output.


def test_hermes_model_id_strips_claude_code_suffix():
    from puffoagent.agent.adapters.docker_cli import _hermes_model_id
    # d2d2-style input: claude-code's [1m] context-window suffix
    # isn't known to hermes and would be rejected.
    assert _hermes_model_id("claude-opus-4-6[1m]") == "anthropic/claude-opus-4-6"


def test_hermes_model_id_prepends_anthropic_prefix_when_missing():
    from puffoagent.agent.adapters.docker_cli import _hermes_model_id
    assert _hermes_model_id("claude-sonnet-4-6") == "anthropic/claude-sonnet-4-6"


def test_hermes_model_id_keeps_explicit_provider_prefix():
    from puffoagent.agent.adapters.docker_cli import _hermes_model_id
    # If someone already passes the full form, don't double-prefix.
    assert _hermes_model_id("openrouter/anthropic/claude-opus-4-6") == \
        "openrouter/anthropic/claude-opus-4-6"


def test_hermes_model_id_empty_returns_default():
    from puffoagent.agent.adapters.docker_cli import _hermes_model_id
    # Empty / missing model → a sensible default so hermes always
    # gets a concrete --model flag.
    assert _hermes_model_id("").startswith("anthropic/")
    assert _hermes_model_id(None).startswith("anthropic/")  # type: ignore[arg-type]


def test_parse_hermes_reply_first_turn():
    from puffoagent.agent.adapters.docker_cli import _parse_hermes_reply
    stdout = (
        "⚠️  Normalized model 'anthropic/claude-opus-4-6' to 'claude-opus-4-6' for \n"
        "anthropic.\n"
        "\n"
        "session_id: 20260422_214146_02b4d1\n"
        "🚀✨🎯"
    )
    reply, session_id = _parse_hermes_reply(stdout)
    assert reply == "🚀✨🎯"
    assert session_id == "20260422_214146_02b4d1"


def test_parse_hermes_reply_resumed_turn():
    """--continue adds a ``↻ Resumed session`` line before session_id.
    Parser must still pick up the reply after session_id, not confuse
    the resume marker for the reply body."""
    from puffoagent.agent.adapters.docker_cli import _parse_hermes_reply
    stdout = (
        "⚠️  Normalized model 'anthropic/claude-opus-4-6' to 'claude-opus-4-6' for \n"
        "anthropic.\n"
        "↻ Resumed session 20260422_213753_5d42f9 (1 user message, 2 total messages)\n"
        "\n"
        "session_id: 20260422_213753_5d42f9\n"
        "Hello there, how are you?"
    )
    reply, session_id = _parse_hermes_reply(stdout)
    assert reply == "Hello there, how are you?"
    assert session_id == "20260422_213753_5d42f9"


def test_parse_hermes_reply_multiline_body():
    """Replies can span multiple lines; parser should preserve
    newlines between them."""
    from puffoagent.agent.adapters.docker_cli import _parse_hermes_reply
    stdout = (
        "session_id: abc\n"
        "line one\n"
        "line two\n"
        "line three"
    )
    reply, session_id = _parse_hermes_reply(stdout)
    assert reply == "line one\nline two\nline three"
    assert session_id == "abc"


def test_parse_hermes_reply_no_session_id_but_reply_present():
    """Some hermes invocations emit the reply without a
    ``session_id:`` line at all (observed on fresh sessions with
    ``--quiet``). The parser must still extract the reply.
    """
    from puffoagent.agent.adapters.docker_cli import _parse_hermes_reply
    stdout = (
        "⚠️  Normalized model 'anthropic/claude-opus-4-6' to 'claude-opus-4-6' for \n"
        "anthropic.\n"
        "[SILENT]"
    )
    reply, session_id = _parse_hermes_reply(stdout)
    assert reply == "[SILENT]"
    assert session_id == ""  # absent; parser tolerates


def test_parse_hermes_reply_resumed_session_id_captured_without_session_id_line():
    """The ``↻ Resumed session <id>`` line is alone enough to
    capture session_id when no standalone ``session_id:`` line
    follows."""
    from puffoagent.agent.adapters.docker_cli import _parse_hermes_reply
    stdout = (
        "⚠️  Normalized model 'anthropic/claude-opus-4-6' to 'claude-opus-4-6' for \n"
        "anthropic.\n"
        "↻ Resumed session 20260422_222809_425056 (1 user message, 2 total messages)\n"
        "你好 @han.dev！有什么我可以帮你的吗？😊"
    )
    reply, session_id = _parse_hermes_reply(stdout)
    assert reply == "你好 @han.dev！有什么我可以帮你的吗？😊"
    assert session_id == "20260422_222809_425056"


def test_parse_hermes_reply_filters_banner_lines_narrowly():
    """Banner-tail filter (``^[a-z0-9-]+\\.$``) only matches a line
    that is literally one word + a period, like ``anthropic.``.
    Regular reply sentences — even ones ending in a period and
    even one that says ``hermes.`` at the end — are NOT eaten.
    """
    from puffoagent.agent.adapters.docker_cli import _parse_hermes_reply
    stdout = (
        "⚠️  Normalized model 'x/y' to 'y' for \n"
        "anthropic.\n"
        "session_id: sid-123\n"
        "The answer is 42.\n"
        "Further context: hermes.\n"
        "- bullet point\n"
        "- another"
    )
    reply, session_id = _parse_hermes_reply(stdout)
    assert session_id == "sid-123"
    # Full content preserved; the banner `anthropic.` tail-line
    # DOES match the filter and is dropped. Regular prose like
    # "Further context: hermes." has spaces and punctuation so
    # it doesn't match — stays in the reply.
    assert "The answer is 42." in reply
    assert "Further context: hermes." in reply
    assert "- bullet point" in reply
    assert "- another" in reply
    # The banner tail line DID get filtered.
    assert "anthropic." not in reply


def test_stitch_hermes_prompt_first_turn():
    """First turn: system prompt inlined above the user message with
    a visible separator. Hermes has no --system flag for chat -q so
    this is how persona lands in the model's context."""
    from puffoagent.agent.adapters.docker_cli import _stitch_hermes_prompt
    stitched = _stitch_hermes_prompt("You are Puffo.", "hello")
    assert stitched == "You are Puffo.\n\n---\n\nhello"


def test_stitch_hermes_prompt_no_system_passes_through():
    """Empty system prompt → pass user_message through unchanged.
    Don't add a stray separator at the top."""
    from puffoagent.agent.adapters.docker_cli import _stitch_hermes_prompt
    assert _stitch_hermes_prompt("", "hello") == "hello"
    assert _stitch_hermes_prompt(None, "hello") == "hello"  # type: ignore[arg-type]


# ── cli-local rejects harness=hermes ─────────────────────────────────────────


def test_local_cli_rejects_hermes_harness():
    """cli-local doesn't support hermes yet — rejecting at adapter
    construction makes the constraint obvious in the daemon log
    rather than silently doing something confusing."""
    from puffoagent.agent.adapters.local_cli import LocalCLIAdapter
    with pytest.raises(RuntimeError, match="not.+supported.+cli-local"):
        LocalCLIAdapter(
            agent_id="t",
            model="",
            workspace_dir="/tmp/ws",
            claude_dir="/tmp/ws/.claude",
            session_file="/tmp/sess.json",
            mcp_config_file="/tmp/mcp.json",
            agent_home_dir="/tmp/agent",
            harness=HermesHarness(),
        )


def test_local_cli_accepts_claude_code_harness():
    """Sanity check: the constructor's harness check doesn't
    false-positive on the default case."""
    from puffoagent.agent.adapters.local_cli import LocalCLIAdapter
    # Should NOT raise.
    LocalCLIAdapter(
        agent_id="t",
        model="",
        workspace_dir="/tmp/ws",
        claude_dir="/tmp/ws/.claude",
        session_file="/tmp/sess.json",
        mcp_config_file="/tmp/mcp.json",
        agent_home_dir="/tmp/agent",
        harness=ClaudeCodeHarness(),
    )


def test_local_cli_rejects_gemini_cli_harness():
    """gemini-cli has the same cli-local limitation as hermes —
    operator's ``~/.gemini/`` may hold their personal sessions.
    Reject at construction with the same shape of error message."""
    from puffoagent.agent.adapters.local_cli import LocalCLIAdapter
    from puffoagent.agent.harness import GeminiCLIHarness
    with pytest.raises(RuntimeError, match="not.+supported.+cli-local"):
        LocalCLIAdapter(
            agent_id="t",
            model="",
            workspace_dir="/tmp/ws",
            claude_dir="/tmp/ws/.claude",
            session_file="/tmp/sess.json",
            mcp_config_file="/tmp/mcp.json",
            agent_home_dir="/tmp/agent",
            harness=GeminiCLIHarness(),
        )


# ── Gemini CLI helpers (model-id + stdout parser) ────────────────────────────
#
# Pure helpers — no container required. We don't exercise the
# subprocess path here; that needs a live gemini install and a
# Google API key, which belongs in the d2d2 smoke test during
# rollout, not unit tests.


def test_gemini_model_id_default_when_empty():
    from puffoagent.agent.adapters.docker_cli import _gemini_model_id
    assert _gemini_model_id("").startswith("gemini-")
    assert _gemini_model_id(None).startswith("gemini-")  # type: ignore[arg-type]


def test_gemini_model_id_passes_through_explicit_value():
    from puffoagent.agent.adapters.docker_cli import _gemini_model_id
    assert _gemini_model_id("gemini-2.5-flash") == "gemini-2.5-flash"


def test_gemini_model_id_strips_claude_style_context_suffix():
    """Be forgiving of operators copy-pasting claude-style model
    ids — strip the ``[1m]`` 1M-context suffix rather than passing
    it through for gemini to reject."""
    from puffoagent.agent.adapters.docker_cli import _gemini_model_id
    assert _gemini_model_id("gemini-2.5-pro[1m]") == "gemini-2.5-pro"


def test_parse_gemini_reply_happy_path():
    from puffoagent.agent.adapters.docker_cli import _parse_gemini_reply
    stdout = '{"response": "hello from gemini", "stats": {"input_tokens": 5}}'
    reply, session_id, err = _parse_gemini_reply(stdout)
    assert reply == "hello from gemini"
    assert session_id == ""
    assert err == ""


def test_parse_gemini_reply_captures_session_id_at_top_level():
    """Gemini 0.38.2 puts ``session_id`` at the top level of the
    JSON (verified empirically against the container), NOT inside
    ``stats`` despite earlier docs speculation. Parser must match."""
    from puffoagent.agent.adapters.docker_cli import _parse_gemini_reply
    stdout = (
        '{"session_id": "d21ddcdd-b12b-4579-9905-9dd0c26beb95", '
        '"response": "OK", "stats": {"models": {}}}'
    )
    reply, session_id, err = _parse_gemini_reply(stdout)
    assert reply == "OK"
    assert session_id == "d21ddcdd-b12b-4579-9905-9dd0c26beb95"


def test_parse_gemini_reply_extracts_message_from_error_object():
    """On structured failures gemini returns
    ``{"session_id": ..., "error": {"type": "Error",
    "message": "...", "code": 1}}`` — parser should surface the
    inner ``message`` string, not stringify the whole dict."""
    from puffoagent.agent.adapters.docker_cli import _parse_gemini_reply
    stdout = (
        '{"session_id": "abc", "error": {"type": "Error", '
        '"message": "You have exhausted your daily quota on this model.", '
        '"code": 1}}'
    )
    reply, session_id, err = _parse_gemini_reply(stdout)
    assert reply == ""
    assert session_id == "abc"
    assert err == "You have exhausted your daily quota on this model."


def test_parse_gemini_reply_flags_usage_banner_as_malformed_argv():
    """If gemini prints its ``Usage: gemini [options]`` help banner
    instead of JSON, our argv was malformed. Returning the banner
    as the reply would leak to the Mattermost channel — instead we
    return empty reply with a clear error string so the worker
    logs it and stays silent."""
    from puffoagent.agent.adapters.docker_cli import _parse_gemini_reply
    stdout = (
        "Usage: gemini [options] [command]\n\n"
        "Gemini CLI - Defaults to interactive mode...\n"
        "Commands:\n  gemini mcp    Manage MCP servers\n"
    )
    reply, session_id, err = _parse_gemini_reply(stdout)
    assert reply == ""
    assert session_id == ""
    assert "argv" in err.lower() and "malformed" in err.lower()


def test_parse_gemini_reply_falls_back_to_raw_on_json_error():
    """Some upstream failures print plain text despite
    ``--output-format json``. Return the raw stdout so the caller
    still has something to log instead of an empty reply."""
    from puffoagent.agent.adapters.docker_cli import _parse_gemini_reply
    stdout = "ERROR: invalid API key"
    reply, session_id, err = _parse_gemini_reply(stdout)
    assert reply == "ERROR: invalid API key"
    assert session_id == ""
    assert err == ""


def test_parse_gemini_reply_empty_stdout_returns_empty():
    from puffoagent.agent.adapters.docker_cli import _parse_gemini_reply
    assert _parse_gemini_reply("") == ("", "", "")
    assert _parse_gemini_reply("   \n  ") == ("", "", "")


def test_parse_gemini_reply_tolerates_missing_response_field():
    """If the JSON is well-formed but lacks ``response``, return
    empty rather than crashing. Lets the caller log the oddity
    and surface a useful error to the channel."""
    from puffoagent.agent.adapters.docker_cli import _parse_gemini_reply
    stdout = '{"stats": {"tokens": 10}}'
    reply, session_id, err = _parse_gemini_reply(stdout)
    assert reply == ""
    assert err == ""


# ── _build_gemini_argv — argv invariants ─────────────────────────────────────
#
# The key regression: our preamble lines (built by
# PuffoAgent._append_user) all start with ``- `` (markdown list
# syntax). Passing that as a separate argv after ``-p`` makes yargs
# think it's another flag, and gemini then prints its --help banner
# + exits rc=0 with empty stdout — which before this fix landed in
# Mattermost as a confusing "Usage: gemini [options]..." reply.


def test_build_gemini_argv_uses_prompt_equals_form_not_dash_p():
    """Load-bearing invariant: the prompt goes in as ``--prompt=<msg>``
    one argv token, not ``-p <msg>`` two tokens. The ``=``-joined
    form tells yargs everything after ``=`` is the value, so a
    leading ``-`` in the value doesn't get eaten as another flag."""
    from puffoagent.agent.adapters.docker_cli import _build_gemini_argv
    argv = _build_gemini_argv(
        container_name="puffo-abc",
        api_key="sk-test",
        model="gemini-2.5-flash",
        has_prior_session=False,
        user_message="- message: hello",
    )
    # The prompt must live in ONE argv token, `=`-joined.
    assert "--prompt=- message: hello" in argv
    # The bare ``-p`` + separate value form must NOT appear.
    assert "-p" not in argv
    # Sanity: full-value preserved, no splitting on the newline etc.
    prompt_tokens = [a for a in argv if a.startswith("--prompt=")]
    assert len(prompt_tokens) == 1
    assert prompt_tokens[0] == "--prompt=- message: hello"


def test_build_gemini_argv_preserves_multi_line_cjk_prompt():
    """Real-world preamble is multi-line CJK + markdown list form.
    All of it must survive untouched as a single argv element."""
    from puffoagent.agent.adapters.docker_cli import _build_gemini_argv
    msg = (
        "- channel: @han.dev\n"
        "- thread_root_id: k87yuaun7p8o8yis8jxuddojse\n"
        "- message: 测试"
    )
    argv = _build_gemini_argv(
        container_name="puffo-abc",
        api_key="sk-test",
        model="",
        has_prior_session=False,
        user_message=msg,
    )
    assert f"--prompt={msg}" in argv


def test_build_gemini_argv_includes_resume_flag_when_session_exists():
    from puffoagent.agent.adapters.docker_cli import _build_gemini_argv
    argv = _build_gemini_argv(
        container_name="puffo-abc",
        api_key="sk-test",
        model="gemini-2.5-flash",
        has_prior_session=True,
        user_message="hi",
    )
    assert "-r" in argv
    # The ``latest`` value must come right after ``-r``.
    i = argv.index("-r")
    assert argv[i + 1] == "latest"


def test_build_gemini_argv_omits_resume_for_fresh_session():
    from puffoagent.agent.adapters.docker_cli import _build_gemini_argv
    argv = _build_gemini_argv(
        container_name="puffo-abc",
        api_key="sk-test",
        model="gemini-2.5-flash",
        has_prior_session=False,
        user_message="hi",
    )
    assert "-r" not in argv
    assert "latest" not in argv


def test_build_gemini_argv_skips_model_flag_when_empty():
    """Empty model → no ``--model`` flag at all, so gemini uses
    whatever default the container ships with."""
    from puffoagent.agent.adapters.docker_cli import _build_gemini_argv
    argv = _build_gemini_argv(
        container_name="puffo-abc",
        api_key="sk-test",
        model="",
        has_prior_session=False,
        user_message="hi",
    )
    assert "--model" not in argv


def test_build_gemini_argv_passes_api_key_via_docker_exec_e():
    """GEMINI_API_KEY flows through ``docker exec -e`` (container
    env), never through the host's environment — keeps the key
    scoped to this one invocation."""
    from puffoagent.agent.adapters.docker_cli import _build_gemini_argv
    argv = _build_gemini_argv(
        container_name="puffo-abc",
        api_key="sk-ant-xyz",
        model="",
        has_prior_session=False,
        user_message="hi",
    )
    assert "docker" in argv and "exec" in argv
    e_idx = argv.index("-e")
    assert argv[e_idx + 1] == "GEMINI_API_KEY=sk-ant-xyz"
