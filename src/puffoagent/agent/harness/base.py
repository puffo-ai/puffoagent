"""Harness interface.

What a runtime adapter needs from its harness:

  - ``name()``: shown in status output; used by the MCP layer to
    decide which tools make sense.
  - ``supports_claude_specific_tools()``: ``install_skill`` / ``refresh``
    / etc. assume Claude Code's skills dir layout and stream-json
    --resume. Hermes doesn't share those, so we short-circuit those
    MCP tools with a clear error when the harness isn't Claude Code.
  - ``run_turn(...)``: the turn protocol. Each harness owns its own
    session model (Claude Code = persistent stream-json subprocess,
    Hermes = one-shot per turn), but both look the same to the
    adapter — give me a user message + system prompt, hand me back
    a TurnResult.

Runtime adapters still handle credential-linking, HOME override,
docker exec vs host subprocess — the harness doesn't know whether
it's running on a bare host or inside a container.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class HarnessTurn:
    """What the harness needs to execute one turn.

    Decoupled from the higher-level ``TurnContext`` so the harness
    doesn't depend on adapter internals. The runtime adapter
    translates ``TurnContext`` into this shape.
    """
    user_message: str
    system_prompt: str
    # Absolute path of the agent's workspace dir. Claude Code treats
    # this as cwd + project-level .claude/ root. Hermes treats it as
    # cwd for the `hermes chat -q` invocation.
    workspace_dir: str
    # Model id (empty = harness / daemon default). Claude: forwarded
    # via --model. Hermes: forwarded via --model provider/name form.
    model: str


class Harness(ABC):
    """Agent engine abstraction. See module docstring."""

    @abstractmethod
    def name(self) -> str:
        """Stable identifier — ``"claude-code"`` / ``"hermes"``."""

    def supports_claude_specific_tools(self) -> bool:
        """True when this harness uses the Claude Code skills-dir
        format + ``--resume`` session protocol. Gates
        install_skill / uninstall_skill / list_skills / refresh and
        the project-scope MCP-server config the agent can write.
        Default False so new harnesses opt in deliberately."""
        return False
