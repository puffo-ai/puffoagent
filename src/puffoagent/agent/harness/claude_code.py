"""Claude Code harness — the original and default.

Exists as a distinct class so:

  - ``harness=claude-code`` is explicit in agent.yml (even when
    it's the default);
  - future adapters can dispatch on the harness identity;
  - ``supports_claude_specific_tools()`` returns True, which is the
    real-today case we want to preserve for ``install_skill``,
    ``refresh``, and the project-scope MCP-server config.

The actual turn-protocol plumbing still lives in
``agent/adapters/cli_session.py`` (stream-json subprocess,
``--resume``, session-id persistence). That's deep enough in the
claude CLI semantics that pulling it up into the harness doesn't
pay for itself yet — the adapter constructs a ``ClaudeSession``
directly when ``harness.name() == "claude-code"``.

If a third claude-like harness ever arrives, that's when it pays
to lift ``ClaudeSession`` behind the harness boundary.
"""

from __future__ import annotations

from .base import Harness


class ClaudeCodeHarness(Harness):
    def name(self) -> str:
        return "claude-code"

    def supports_claude_specific_tools(self) -> bool:
        return True

    def supported_providers(self) -> frozenset[str]:
        # Claude Code is Anthropic-only. Routing through a proxy to
        # a non-Anthropic model is not a supported configuration.
        return frozenset({"anthropic"})
