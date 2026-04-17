"""Adapter interface.

An adapter is a thin translation layer between the portal and an
external agent runtime. The runtime (the claude-agent-sdk package, the
claude CLI, etc.) owns the agentic loop and the tool catalog; the
adapter just translates ``TurnContext`` into the runtime's native
invocation, forwards its output back as a ``TurnResult``, and manages
the runtime instance's lifecycle.

See ``DESIGN.md`` at the repo root for the full responsibility split.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional


ProgressCallback = Callable[[str], Awaitable[None]]


@dataclass
class TurnContext:
    """Everything an adapter needs to run one turn of conversation.

    ``workspace_dir`` / ``claude_dir`` / ``memory_dir`` are absolute
    paths the adapter may bind-mount or pass to its runtime. Not every
    adapter uses every field — chat-only adapters ignore the directory
    fields entirely.
    """
    system_prompt: str
    messages: list[dict]
    workspace_dir: str = ""
    claude_dir: str = ""
    memory_dir: str = ""
    on_progress: Optional[ProgressCallback] = None


@dataclass
class TurnResult:
    """Everything the shell needs back from an adapter to finish a turn.

    ``reply`` of ``""`` or ``"[SILENT]"`` means the agent chose not to
    respond; the shell translates both to "don't post to Mattermost".
    """
    reply: str
    input_tokens: int = 0
    output_tokens: int = 0
    tool_calls: int = 0
    metadata: dict = field(default_factory=dict)


class Adapter(ABC):
    """Base class for all runtime adapters."""

    @abstractmethod
    async def run_turn(self, ctx: TurnContext) -> TurnResult:
        """Execute one turn against the underlying runtime."""

    async def warm(self, system_prompt: str) -> None:
        """Pre-spawn any long-lived runtime state so the first turn
        doesn't pay startup latency. The worker calls this right after
        construction if the agent already has a persisted session (so
        it can ``--resume`` immediately on daemon start).

        Default is a no-op for stateless adapters (chat-only, sdk).
        """
        return None

    async def aclose(self) -> None:
        """Release any runtime resources (containers, subprocesses, MCP
        servers). Default is a no-op for stateless adapters.
        """
        return None


def format_history_as_prompt(messages: list[dict]) -> str:
    """Render shell conversation history as a single prompt string.

    The SDK adapter is one-shot per turn; the two CLI adapters keep a
    long-lived subprocess that owns its own session, so they pass
    only the latest user message (see local_cli / docker_cli). This
    helper is therefore only used by the SDK adapter today, but kept
    in the base module for any future adapter that wants to embed
    history in a single prompt string.
    """
    if not messages:
        return ""
    if len(messages) == 1:
        return messages[0]["content"]
    parts = ["<prior_turns>"]
    for m in messages[:-1]:
        parts.append(f"[{m['role']}]\n{m['content']}")
    parts.append("</prior_turns>")
    parts.append(messages[-1]["content"])
    return "\n\n".join(parts)
