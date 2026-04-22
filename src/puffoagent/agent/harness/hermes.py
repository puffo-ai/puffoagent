"""Hermes harness — Nous Research's agent engine.

Unlike Claude Code, Hermes doesn't have a long-lived stream-json
subprocess with ``--resume``. Each turn is a fresh ``hermes chat -q
"<message>"`` invocation; session state is managed by Hermes itself
through files under ``~/.hermes/sessions/``.

Authentication: Hermes auto-discovers Claude Code's credential file
(``$HOME/.claude/.credentials.json``) and uses it to call the
Anthropic API directly. No second credential path needed — our
existing ``link_host_credentials`` puts the file where Hermes looks.

Billing caveat (upstream known issue, repeat here so it's obvious
in code review): Anthropic routes third-party OAuth clients to the
``extra_usage`` billing pool, NOT the Claude subscription. Same
token, different ledger. See NousResearch/hermes-agent#12905.

Since Hermes uses its own skill system (``~/.hermes/skills/``) and
session-id scheme, the Claude-Code-specific MCP tools
(install_skill, refresh, etc.) are disabled when this harness is
active — ``supports_claude_specific_tools()`` stays False.
"""

from __future__ import annotations

from .base import Harness


class HermesHarness(Harness):
    def name(self) -> str:
        return "hermes"
    # supports_claude_specific_tools() → False (inherited default).
