"""Chat-only adapter.

Wraps today's Anthropic/OpenAI message-completion providers so existing
agents keep working while the rest of the adapter work lands. This
adapter does NOT run tools, does NOT touch the filesystem, and ignores
``workspace_dir`` / ``claude_dir``. It is deliberately a migration
seam — once the ``sdk`` adapter is stable, this one can go away.
"""

from __future__ import annotations

import asyncio

from .base import Adapter, TurnContext, TurnResult


class ChatOnlyAdapter(Adapter):
    def __init__(self, provider):
        # ``provider`` is any object with a blocking
        # ``complete(system_prompt, messages) -> (str, int, int)`` — i.e.
        # AnthropicProvider or OpenAIProvider.
        self._provider = provider

    async def run_turn(self, ctx: TurnContext) -> TurnResult:
        reply, input_tokens, output_tokens = await asyncio.to_thread(
            self._provider.complete, ctx.system_prompt, ctx.messages,
        )
        return TurnResult(
            reply=reply,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            tool_calls=0,
        )
