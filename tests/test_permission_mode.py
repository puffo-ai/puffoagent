"""Unit tests for permission_mode validation + wiring into the
cli-local command line. Guards against silent drift: a bad mode
name in agent.yml should fall back to 'default' with a WARNING,
never silently run with some other mode.
"""

import logging

import pytest

from puffoagent.agent.adapters.local_cli import (
    VALID_PERMISSION_MODES,
    _sanitise_permission_mode,
    LocalCLIAdapter,
)


# ── _sanitise_permission_mode ────────────────────────────────────────────────


class TestSanitisePermissionMode:
    @pytest.mark.parametrize("mode", [
        "default", "acceptEdits", "auto", "dontAsk", "bypassPermissions",
    ])
    def test_known_modes_pass_through(self, mode):
        assert _sanitise_permission_mode(mode, "a") == mode

    def test_empty_defaults_to_default(self):
        assert _sanitise_permission_mode("", "a") == "default"

    def test_unknown_mode_falls_back_to_default(self, caplog):
        with caplog.at_level(logging.WARNING):
            result = _sanitise_permission_mode("paranoid", "han-local-cli")
        assert result == "default"
        assert any(
            "unknown permission_mode" in r.message and "paranoid" in r.message
            for r in caplog.records
        ), "expected a WARNING log on unknown mode"

    def test_plan_mode_rejected(self):
        # We deliberately don't support 'plan' — it's a research
        # mode, not useful for a chat-reply agent. Silent fallback
        # to 'default' keeps the user's setup working while making
        # the misconfiguration visible in the log.
        assert _sanitise_permission_mode("plan", "a") == "default"

    def test_case_sensitive(self):
        # claude-code expects exact casing; "Default" is not valid.
        assert _sanitise_permission_mode("Default", "a") == "default"

    def test_valid_set_excludes_plan(self):
        # Sanity check: VALID_PERMISSION_MODES should NOT contain
        # 'plan' — if someone adds it back, this test fires and
        # forces a conscious revisit.
        assert "plan" not in VALID_PERMISSION_MODES

    def test_valid_set_has_five_modes(self):
        assert len(VALID_PERMISSION_MODES) == 5


# ── _build_command ──────────────────────────────────────────────────────────


def _make_adapter(permission_mode: str = "default", model: str = "") -> LocalCLIAdapter:
    # tmp paths — we never spawn, so they don't need to exist.
    return LocalCLIAdapter(
        agent_id="a",
        model=model,
        workspace_dir="/tmp/ws",
        claude_dir="/tmp/ws/.claude",
        session_file="/tmp/a/cli_session.json",
        mcp_config_file="/tmp/a/mcp-config.json",
        agent_home_dir="/tmp/a",
        permission_mode=permission_mode,
    )


class TestBuildCommand:
    def test_command_starts_with_claude_and_permission_mode(self):
        adapter = _make_adapter(permission_mode="default")
        cmd = adapter._build_command(extra_args=[])
        assert cmd[0] == "claude"
        # --permission-mode <value> must appear; order doesn't matter
        # but it should appear before any user extra_args so claude
        # picks it up.
        assert "--permission-mode" in cmd
        i = cmd.index("--permission-mode")
        assert cmd[i + 1] == "default"

    def test_model_flag_included_when_set(self):
        adapter = _make_adapter(permission_mode="default", model="claude-opus-4-6")
        cmd = adapter._build_command(extra_args=[])
        assert "--model" in cmd
        assert "claude-opus-4-6" in cmd

    def test_model_flag_omitted_when_empty(self):
        adapter = _make_adapter(permission_mode="default", model="")
        cmd = adapter._build_command(extra_args=[])
        assert "--model" not in cmd

    def test_bypass_permissions_passes_through(self):
        adapter = _make_adapter(permission_mode="bypassPermissions")
        cmd = adapter._build_command(extra_args=[])
        i = cmd.index("--permission-mode")
        assert cmd[i + 1] == "bypassPermissions"

    def test_never_passes_dangerously_skip_permissions(self):
        # Regression guard — we deprecated this flag in favour of
        # --permission-mode. If it creeps back in, permission
        # decisions go silent-bypass and our MCP proxy never fires.
        for mode in VALID_PERMISSION_MODES:
            adapter = _make_adapter(permission_mode=mode)
            cmd = adapter._build_command(extra_args=["--foo", "bar"])
            assert "--dangerously-skip-permissions" not in cmd

    def test_unknown_mode_sanitised_at_construction(self, caplog):
        with caplog.at_level(logging.WARNING):
            adapter = _make_adapter(permission_mode="lolwut")
        assert adapter.permission_mode == "default"
        cmd = adapter._build_command(extra_args=[])
        i = cmd.index("--permission-mode")
        assert cmd[i + 1] == "default"

    def test_extra_args_preserved(self):
        adapter = _make_adapter(permission_mode="default")
        cmd = adapter._build_command(extra_args=["--mcp-config", "/x.json"])
        assert cmd[-2] == "--mcp-config"
        assert cmd[-1] == "/x.json"
