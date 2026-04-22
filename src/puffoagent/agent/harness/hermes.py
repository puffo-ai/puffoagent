"""Hermes harness — Nous Research's agent engine.

Unlike Claude Code, Hermes has no stream-json protocol we can drive
over a pipe. Its interactive mode requires a real TTY, and piping
stdin makes it treat EOF as "user quit". The supported programmatic
path is one-shot ``hermes chat -q <message>`` per turn; multi-turn
continuity is hermes' responsibility, persisted in
``~/.hermes/state.db`` and resumed via ``--continue``.

The DockerCLIAdapter's ``_run_turn_hermes`` implementation therefore
spawns a fresh ``docker exec <container> hermes chat --provider
anthropic --quiet --continue -q <prompt>`` per turn. Cold start is
~3-7 s; from Mattermost's POV the user experience is the same as
Claude Code (they type, they wait, they get a contextual reply).

Authentication: zero-config. Hermes auto-discovers Claude Code's
credential file at ``$HOME/.claude/.credentials.json`` (already
bind-mounted by the cli-docker adapter) and uses the access token
to call the Anthropic API directly. No second credential path, no
``hermes auth add`` step needed.

Billing caveat (upstream known issue, repeat here so it's obvious
in code review): Anthropic routes third-party OAuth clients to the
``extra_usage`` billing pool, NOT the Claude subscription. Same
token, different ledger. See NousResearch/hermes-agent#12905.

**Runtime support:** cli-docker only. cli-local rejects
``harness=hermes`` at adapter construction — replicating the
containerised hermes setup on the operator's bare host (where
``~/.hermes/`` may contain the operator's own personal hermes
sessions) needs its own design round. See
``LocalCLIAdapter.__init__`` for the guard.

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
