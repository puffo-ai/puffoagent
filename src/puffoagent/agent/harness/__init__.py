"""Harness abstraction: which agent engine runs inside a runtime.

Runtime (cli-local / cli-docker / sdk / chat-only) answers *where*
the agent executes. Harness answers *what* executes there. The
default harness for every CLI runtime is Claude Code — the claude
binary spawned with our stream-json session protocol. A second
harness, Hermes, spawns `hermes chat -q` one-shot per turn against
the Anthropic API using Claude Code's credential store.

Keeping the two concerns separate (before a third harness arrives)
avoids the combinatorial mess of adapter classes named after their
harness + runtime.
"""

from .base import Harness, HarnessTurn
from .claude_code import ClaudeCodeHarness
from .hermes import HermesHarness


def build_harness(name: str) -> Harness:
    """Resolve a harness name from agent.yml into an implementation.
    Defaults to Claude Code — existing agents without the field set
    behave exactly as before.
    """
    if not name or name == "claude-code":
        return ClaudeCodeHarness()
    if name == "hermes":
        return HermesHarness()
    raise ValueError(
        f"unknown harness {name!r}: expected one of 'claude-code', 'hermes'"
    )


__all__ = [
    "Harness",
    "HarnessTurn",
    "ClaudeCodeHarness",
    "HermesHarness",
    "build_harness",
]
