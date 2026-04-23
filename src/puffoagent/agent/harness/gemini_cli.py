"""Gemini CLI harness — reserved.

Google ships a ``gemini`` CLI that targets the Gemini model family
natively (auth, session persistence, tool calls). Wiring it into
puffoagent would follow the same shape as the claude-code / hermes
adapters: a subprocess on the host (cli-local) or inside a docker
container (cli-docker), a session-continuation mechanism, and a
per-agent ``~/.gemini/`` home.

This class is declared now so the validation matrix in
``portal/runtime_matrix.py`` can reject impossible triples like
``harness=gemini-cli`` + ``provider=anthropic`` even before the
adapter plumbing lands. The actual turn protocol raises
``NotImplementedError`` — configuring an agent with
``runtime.harness=gemini-cli`` will fail fast at adapter
construction with a clear "not yet implemented" error rather than
silently falling back to claude-code.
"""

from __future__ import annotations

from .base import Harness


class GeminiCLIHarness(Harness):
    def name(self) -> str:
        return "gemini-cli"

    def supported_providers(self) -> frozenset[str]:
        # Gemini CLI is Google-only by design.
        return frozenset({"google"})

    # supports_claude_specific_tools() → False (inherited default).
