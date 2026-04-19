"""Unit tests for pure helpers in puffo_tools. These cover the
functions that don't need a live Mattermost — post-id parsing,
tool-input summarisation. Regression coverage for formatting /
parsing drift.
"""

import pytest

from puffoagent.mcp.puffo_tools import _parse_post_ref, _summarise_tool_input


# ── _parse_post_ref ──────────────────────────────────────────────────────────


class TestParsePostRef:
    def test_bare_id_passes_through(self):
        pid = "abcdefghijklmnopqrstuvwxyz"  # 26 chars
        assert _parse_post_ref(pid) == pid

    def test_permalink_with_team(self):
        url = "https://app.puffo.ai/mycore/pl/abcdefghijklmnopqrstuvwxyz"
        assert _parse_post_ref(url) == "abcdefghijklmnopqrstuvwxyz"

    def test_permalink_with_trailing_slash(self):
        url = "https://app.puffo.ai/mycore/pl/abcdefghijklmnopqrstuvwxyz/"
        assert _parse_post_ref(url) == "abcdefghijklmnopqrstuvwxyz"

    def test_permalink_with_query_fragment(self):
        url = "https://app.puffo.ai/mycore/pl/abcdefghijklmnopqrstuvwxyz?ref=x"
        assert _parse_post_ref(url) == "abcdefghijklmnopqrstuvwxyz"

    def test_whitespace_stripped(self):
        assert _parse_post_ref("  abcdefghijklmnopqrstuvwxyz  ") == "abcdefghijklmnopqrstuvwxyz"

    def test_invalid_length_raises(self):
        with pytest.raises(RuntimeError, match="cannot parse post ref"):
            _parse_post_ref("short")

    def test_uppercase_raises(self):
        # Mattermost post ids are lowercase alphanumeric only.
        with pytest.raises(RuntimeError, match="cannot parse post ref"):
            _parse_post_ref("ABCDEFGHIJKLMNOPQRSTUVWXYZ")

    def test_empty_raises(self):
        with pytest.raises(RuntimeError):
            _parse_post_ref("")

    def test_url_without_pl_segment_raises(self):
        with pytest.raises(RuntimeError):
            _parse_post_ref("https://app.puffo.ai/mycore/messages/abc")


# ── _summarise_tool_input ────────────────────────────────────────────────────


class TestSummariseToolInput:
    def test_dict_renders_as_bullet_list(self):
        out = _summarise_tool_input({"path": "/tmp/x.txt", "lines": 20})
        assert "**path**" in out
        assert "`/tmp/x.txt`" in out
        assert "**lines**" in out
        assert "`20`" in out

    def test_long_value_truncated_per_key(self):
        long_val = "x" * 500
        out = _summarise_tool_input({"cmd": long_val})
        # Per-value cap is 120 chars + ellipsis marker.
        assert "xxxxxxxxxx…" in out
        # Total should be short enough to fit in a DM without walls.
        assert len(out) < 300

    def test_string_input_renders_as_single_code_span(self):
        out = _summarise_tool_input("rm -rf /")
        assert "`rm -rf /`" == out or "rm -rf /" in out

    def test_empty_dict_placeholder(self):
        out = _summarise_tool_input({})
        # Empty dict should still produce SOMETHING — the owner needs
        # to see that there was no meaningful input rather than a
        # blank DM.
        assert out

    def test_none_placeholder(self):
        out = _summarise_tool_input(None)
        assert out

    def test_final_limit_applied(self):
        # Many keys each under 120 chars but totalling over 400 should
        # still get truncated to the overall limit + ellipsis.
        data = {f"k{i}": "v" * 50 for i in range(20)}
        out = _summarise_tool_input(data, limit=400)
        assert len(out) <= 400 + 1  # ellipsis char can push by one
