"""Runtime adapters.

Each adapter is a thin translation layer between the portal's shell
(``PuffoAgent``) and an external agent runtime — the Anthropic/OpenAI
Messages API, the ``claude-agent-sdk`` package, or the ``claude`` CLI
binary. Adapters do not implement tools or run the agentic loop
themselves; they configure the runtime, forward its output, and manage
its lifecycle. See ``base.py`` for the interface.
"""

from .base import Adapter, TurnContext, TurnResult

__all__ = ["Adapter", "TurnContext", "TurnResult"]
