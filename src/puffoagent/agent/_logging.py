"""Shared per-agent logging helper.

Every subsystem the worker owns (MattermostClient, PuffoAgent,
adapters) lives inside exactly one agent. When the daemon
hosts many agents their log output interleaves and becomes hard to
follow. ``agent_logger`` returns a ``LoggerAdapter`` that prefixes
each record with ``agent <id>:`` so every line is unambiguously
attributable.

Usage:

    from ._logging import agent_logger
    logger = agent_logger(__name__, agent_id)
    logger.info("hello")  # → "agent han-copy: hello"
"""

from __future__ import annotations

import logging


class _AgentLogAdapter(logging.LoggerAdapter):
    def process(self, msg, kwargs):
        agent_id = self.extra.get("agent_id") if self.extra else ""
        if agent_id:
            return f"agent {agent_id}: {msg}", kwargs
        return msg, kwargs


def agent_logger(name: str, agent_id: str) -> logging.LoggerAdapter:
    return _AgentLogAdapter(logging.getLogger(name), {"agent_id": agent_id})
