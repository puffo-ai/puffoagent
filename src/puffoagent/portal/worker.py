"""Per-agent worker that runs one MattermostClient loop.

Each running agent gets a single asyncio.Task owning a `Worker` instance.
The worker:
  - instantiates PuffoAgent + MattermostClient from the agent's config
  - runs the WebSocket listen loop with supervising backoff on errors
  - writes runtime.json every `runtime_heartbeat_seconds` so the CLI can
    show live stats without any IPC
  - cancels cleanly when the reconciler decides the agent should stop
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from ..agent.core import PuffoAgent
from ..agent.file_browser import FileBrowser
from ..agent.mattermost_client import MattermostClient
from .state import (
    AgentConfig,
    DaemonConfig,
    RuntimeState,
    agent_dir,
)

logger = logging.getLogger(__name__)

RECONNECT_BACKOFF_SECONDS = 5.0


def build_provider(daemon_cfg: DaemonConfig, agent_cfg: AgentConfig):
    """Resolve the AI provider for an agent, preferring per-agent overrides."""
    provider_name = agent_cfg.ai.provider or daemon_cfg.default_provider

    if provider_name == "anthropic":
        from ..agent.providers.anthropic_provider import AnthropicProvider
        api_key = agent_cfg.ai.api_key or daemon_cfg.anthropic.api_key
        model = agent_cfg.ai.model or daemon_cfg.anthropic.model or "claude-sonnet-4-6"
        if not api_key:
            raise RuntimeError(
                f"agent {agent_cfg.id!r}: anthropic api_key is not set in "
                "daemon.yml or agent.yml"
            )
        return AnthropicProvider(api_key=api_key, model=model)

    if provider_name == "openai":
        from ..agent.providers.openai_provider import OpenAIProvider
        api_key = agent_cfg.ai.api_key or daemon_cfg.openai.api_key
        model = agent_cfg.ai.model or daemon_cfg.openai.model or "gpt-4o"
        if not api_key:
            raise RuntimeError(
                f"agent {agent_cfg.id!r}: openai api_key is not set in "
                "daemon.yml or agent.yml"
            )
        return OpenAIProvider(api_key=api_key, model=model)

    raise RuntimeError(f"agent {agent_cfg.id!r}: unknown provider {provider_name!r}")


class Worker:
    """Runs a single AI agent inside the daemon event loop."""

    def __init__(self, daemon_cfg: DaemonConfig, agent_cfg: AgentConfig):
        self.daemon_cfg = daemon_cfg
        self.agent_cfg = agent_cfg
        self.runtime = RuntimeState(
            status="running",
            started_at=int(time.time()),
            msg_count=0,
        )
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    def start(self) -> asyncio.Task:
        """Launch the main coroutine as a task. Idempotent."""
        if self._task is not None and not self._task.done():
            return self._task
        self._task = asyncio.ensure_future(self._run())
        return self._task

    async def stop(self) -> None:
        """Request the worker to stop and await its exit."""
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self.runtime.status = "stopped"
        self.runtime.save(self.agent_cfg.id)

    async def _run(self) -> None:
        """Main loop: instantiate agent + client, run listen() with backoff."""
        agent_id = self.agent_cfg.id
        try:
            provider = build_provider(self.daemon_cfg, self.agent_cfg)
            profile_path = str(self.agent_cfg.resolve_profile_path())
            memory_path = str(self.agent_cfg.resolve_memory_dir())
            skills_dir = self.daemon_cfg.skills_dir or ""
            Path(memory_path).mkdir(parents=True, exist_ok=True)

            puffo = PuffoAgent(
                provider=provider,
                profile_path=profile_path,
                memory_dir=memory_path,
                skills_dir=skills_dir,
            )
            client = MattermostClient(
                url=self.agent_cfg.mattermost.url,
                token=self.agent_cfg.mattermost.bot_token,
                profile_name=Path(profile_path).stem,
                file_server_url="",
            )
            client.set_rpc_handler(FileBrowser(str(agent_dir(agent_id))))
        except Exception as e:
            logger.error("agent %s: failed to initialise: %s", agent_id, e, exc_info=True)
            self.runtime.status = "error"
            self.runtime.error = str(e)
            self.runtime.save(agent_id)
            return

        async def on_message(channel_id, channel_name, sender, sender_email, text, root_id, direct):
            typing_task = asyncio.ensure_future(_keep_typing(client, channel_id, root_id))
            try:
                reply = await asyncio.to_thread(
                    puffo.handle_message,
                    channel_id, channel_name, sender, sender_email, text, direct,
                )
            except Exception as exc:
                logger.error("agent %s: handle_message error: %s", agent_id, exc, exc_info=True)
                reply = None
            finally:
                typing_task.cancel()
            self.runtime.msg_count += 1
            self.runtime.last_event_at = int(time.time())
            if reply:
                await client.post_message(channel_id, reply, root_id=root_id)

        # runtime.json heartbeat (purely for the CLI)
        async def heartbeat():
            interval = max(1.0, self.daemon_cfg.runtime_heartbeat_seconds)
            while not self._stop.is_set():
                self.runtime.save(agent_id)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=interval)
                except asyncio.TimeoutError:
                    pass

        hb_task = asyncio.ensure_future(heartbeat())
        try:
            while not self._stop.is_set():
                try:
                    await client.listen(on_message=on_message)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "agent %s: listen() crashed: %s — reconnecting in %.1fs",
                        agent_id, exc, RECONNECT_BACKOFF_SECONDS,
                    )
                    self.runtime.error = str(exc)
                    self.runtime.save(agent_id)
                if self._stop.is_set():
                    break
                # Backoff between reconnect attempts.
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=RECONNECT_BACKOFF_SECONDS)
                except asyncio.TimeoutError:
                    pass
        finally:
            hb_task.cancel()
            try:
                await hb_task
            except (asyncio.CancelledError, Exception):
                pass
            self.runtime.status = "stopped"
            self.runtime.save(agent_id)


async def _keep_typing(client: MattermostClient, channel_id: str, parent_id: str) -> None:
    """Send typing indicator every 4 seconds until cancelled."""
    try:
        while True:
            await client.send_typing(channel_id, parent_id)
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        pass
