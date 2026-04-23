"""Harness abstraction: which agent engine runs inside a runtime.

Runtime (``cli-local`` / ``cli-docker`` / ``sdk-local`` /
``chat-local``) answers *where* the agent executes. Harness answers
*what* executes there — only meaningful for the CLI runtimes.
``chat-local`` and ``sdk-local`` ignore the field since the SDK /
plain API is already the agent engine.

Three harnesses are declared:

  - ``claude-code`` — Claude Code CLI (Anthropic only).
  - ``hermes`` — Nous Research hermes CLI (Anthropic + OpenAI).
  - ``gemini-cli`` — Google's gemini CLI (Google only). Declared
    now for matrix validation; adapter plumbing lands later.

Each declares its ``supported_providers`` so the runtime matrix in
``portal/runtime_matrix.py`` can reject mismatched triples (e.g.
``harness=claude-code`` + ``provider=google``) at load time.
"""

from .base import Harness, HarnessTurn
from .claude_code import ClaudeCodeHarness
from .gemini_cli import GeminiCLIHarness
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
    if name == "gemini-cli":
        return GeminiCLIHarness()
    raise ValueError(
        f"unknown harness {name!r}: expected one of "
        "'claude-code', 'hermes', 'gemini-cli'"
    )


__all__ = [
    "Harness",
    "HarnessTurn",
    "ClaudeCodeHarness",
    "GeminiCLIHarness",
    "HermesHarness",
    "build_harness",
]
