"""Per-agent worker that runs one MattermostClient loop.

Each running agent gets a single asyncio.Task owning a `Worker` instance.
The worker:
  - instantiates PuffoAgent + an Adapter + MattermostClient from the
    agent's config
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

from ..agent.adapters import Adapter
from ..agent.core import PuffoAgent
from ..agent.file_browser import FileBrowser
from ..agent.mattermost_client import MattermostClient
from ..agent.shared_content import (
    assemble_claude_md,
    ensure_shared_primer,
    read_memory_snapshot,
    read_shared_primer,
    sync_shared_skills,
    write_claude_md,
)
from .state import (
    AgentConfig,
    DaemonConfig,
    RuntimeConfig,
    RuntimeState,
    agent_dir,
    agent_home_dir,
    cli_session_json_path,
    docker_dir,
    docker_shared_dir,
    shared_fs_dir,
)

logger = logging.getLogger(__name__)

RECONNECT_BACKOFF_SECONDS = 5.0


def build_adapter(daemon_cfg: DaemonConfig, agent_cfg: AgentConfig) -> Adapter:
    """Select and construct the adapter for an agent based on
    ``runtime.kind``. Raises if the selected kind is unknown or
    misconfigured.
    """
    kind = agent_cfg.runtime.kind or "chat-only"

    if kind == "chat-only":
        from ..agent.adapters.chat_only import ChatOnlyAdapter
        provider = _build_legacy_provider(daemon_cfg, agent_cfg.runtime)
        return ChatOnlyAdapter(provider)

    if kind == "sdk":
        from ..agent.adapters.sdk import SDKAdapter
        api_key = agent_cfg.runtime.api_key or daemon_cfg.anthropic.api_key
        model = agent_cfg.runtime.model or daemon_cfg.anthropic.model or "claude-sonnet-4-6"
        if not api_key:
            raise RuntimeError(
                f"agent {agent_cfg.id!r}: runtime kind 'sdk' requires an anthropic "
                "api_key in daemon.yml or agent.yml"
            )
        return SDKAdapter(
            api_key=api_key,
            model=model,
            allowed_tools=agent_cfg.runtime.allowed_tools,
            agent_id=agent_cfg.id,
            mattermost_url=agent_cfg.mattermost.url,
            mattermost_token=agent_cfg.mattermost.bot_token,
            workspace_dir=str(agent_cfg.resolve_workspace_dir()),
            team=agent_cfg.mattermost.team_name,
        )

    # The claude CLI adapters authenticate via OAuth credentials the
    # user set up once with `claude login` on the host. We do NOT pass
    # an api_key through; the CLI uses ~/.claude/.credentials.json
    # directly. The model override still flows through for users who
    # want a specific claude model per agent.
    if kind == "cli-docker":
        from ..agent.adapters.docker_cli import DockerCLIAdapter
        return DockerCLIAdapter(
            agent_id=agent_cfg.id,
            model=agent_cfg.runtime.model or daemon_cfg.anthropic.model or "",
            image=agent_cfg.runtime.docker_image,
            workspace_dir=str(agent_cfg.resolve_workspace_dir()),
            claude_dir=str(agent_cfg.resolve_claude_dir()),
            session_file=str(cli_session_json_path(agent_cfg.id)),
            agent_home_dir=str(agent_home_dir(agent_cfg.id)),
            shared_fs_dir=str(shared_fs_dir()),
            mcp_script_dir=str(docker_dir() / "mcp"),
            mattermost_url=agent_cfg.mattermost.url,
            mattermost_token=agent_cfg.mattermost.bot_token,
            team=agent_cfg.mattermost.team_name,
        )

    if kind == "cli-local":
        from ..agent.adapters.local_cli import LocalCLIAdapter
        return LocalCLIAdapter(
            agent_id=agent_cfg.id,
            model=agent_cfg.runtime.model or daemon_cfg.anthropic.model or "",
            workspace_dir=str(agent_cfg.resolve_workspace_dir()),
            claude_dir=str(agent_cfg.resolve_claude_dir()),
            session_file=str(cli_session_json_path(agent_cfg.id)),
            mcp_config_file=str(agent_dir(agent_cfg.id) / "mcp-config.json"),
            agent_home_dir=str(agent_home_dir(agent_cfg.id)),
            mattermost_url=agent_cfg.mattermost.url,
            mattermost_token=agent_cfg.mattermost.bot_token,
            team=agent_cfg.mattermost.team_name,
        )

    raise RuntimeError(
        f"agent {agent_cfg.id!r}: unknown runtime kind {kind!r} "
        "(valid: chat-only, sdk, cli-docker, cli-local)"
    )


def _build_legacy_provider(daemon_cfg: DaemonConfig, runtime: RuntimeConfig):
    """Build an Anthropic/OpenAI message-completion provider for the
    chat-only adapter. Per-agent overrides win over daemon defaults.
    """
    provider_name = runtime.provider or daemon_cfg.default_provider

    if provider_name == "anthropic":
        from ..agent.providers.anthropic_provider import AnthropicProvider
        api_key = runtime.api_key or daemon_cfg.anthropic.api_key
        model = runtime.model or daemon_cfg.anthropic.model or "claude-sonnet-4-6"
        if not api_key:
            raise RuntimeError(
                "anthropic api_key is not set in daemon.yml or agent.yml"
            )
        return AnthropicProvider(api_key=api_key, model=model)

    if provider_name == "openai":
        from ..agent.providers.openai_provider import OpenAIProvider
        api_key = runtime.api_key or daemon_cfg.openai.api_key
        model = runtime.model or daemon_cfg.openai.model or "gpt-4o"
        if not api_key:
            raise RuntimeError(
                "openai api_key is not set in daemon.yml or agent.yml"
            )
        return OpenAIProvider(api_key=api_key, model=model)

    raise RuntimeError(f"unknown provider {provider_name!r}")


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
        self._adapter: Adapter | None = None

    def start(self) -> asyncio.Task:
        if self._task is not None and not self._task.done():
            return self._task
        self._task = asyncio.ensure_future(self._run())
        return self._task

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        if self._adapter is not None:
            try:
                await self._adapter.aclose()
            except Exception as exc:
                logger.warning(
                    "agent %s: adapter aclose failed: %s", self.agent_cfg.id, exc,
                )
        self.runtime.status = "stopped"
        self.runtime.save(self.agent_cfg.id)

    async def _run(self) -> None:
        agent_id = self.agent_cfg.id
        try:
            self._adapter = build_adapter(self.daemon_cfg, self.agent_cfg)
            profile_path = str(self.agent_cfg.resolve_profile_path())
            memory_path = str(self.agent_cfg.resolve_memory_dir())
            workspace_path = str(self.agent_cfg.resolve_workspace_dir())
            claude_path = str(self.agent_cfg.resolve_claude_dir())
            Path(memory_path).mkdir(parents=True, exist_ok=True)
            Path(workspace_path).mkdir(parents=True, exist_ok=True)
            _seed_claude_dir(Path(claude_path))

            # Assemble this agent's CLAUDE.md from the shared primer +
            # profile + memory snapshot. Single source of truth for
            # all three agentic runtimes: SDK/chat-only read this
            # string via PuffoAgent's system-prompt builder, cli-local
            # and cli-docker let Claude Code auto-discover the file
            # via <cwd>/.claude/CLAUDE.md. Regenerates on every worker
            # start so pause/resume picks up edits.
            shared_path = docker_shared_dir()
            ensure_shared_primer(shared_path)
            sync_shared_skills(shared_path, Path(workspace_path))
            primer = read_shared_primer(shared_path)
            try:
                profile_text = Path(profile_path).read_text(encoding="utf-8")
            except OSError:
                profile_text = ""
            claude_md = assemble_claude_md(
                shared_primer=primer,
                profile=profile_text,
                memory_snapshot=read_memory_snapshot(Path(memory_path)),
            )
            write_claude_md(Path(workspace_path), claude_md)

            puffo = PuffoAgent(
                adapter=self._adapter,
                system_prompt=claude_md,
                memory_dir=memory_path,
                workspace_dir=workspace_path,
                claude_dir=claude_path,
                agent_id=agent_id,
            )
            client = MattermostClient(
                url=self.agent_cfg.mattermost.url,
                token=self.agent_cfg.mattermost.bot_token,
                profile_name=Path(profile_path).stem,
                file_server_url="",
                agent_id=agent_id,
                workspace_dir=workspace_path,
            )
            client.set_rpc_handler(FileBrowser(str(agent_dir(agent_id))))
        except Exception as e:
            logger.error("agent %s: failed to initialise: %s", agent_id, e, exc_info=True)
            self.runtime.status = "error"
            self.runtime.error = str(e)
            self.runtime.save(agent_id)
            return

        # Warm the adapter so agents with a persisted session re-spawn
        # their claude subprocess right now rather than on the first
        # DM. Failure here is non-fatal — the warm path is an
        # optimisation, not a requirement — so log and continue.
        try:
            await self._adapter.warm(claude_md)
        except Exception as exc:
            logger.warning(
                "agent %s: warm() failed (will retry on first turn): %s",
                agent_id, exc,
            )

        async def on_message(
            channel_id, channel_name, sender, sender_email, text,
            root_id, direct, attachments, sender_is_bot, mentions,
        ):
            typing_task = asyncio.ensure_future(_keep_typing(client, channel_id, root_id))
            try:
                reply = await puffo.handle_message(
                    channel_id, channel_name, sender, sender_email, text, direct,
                    attachments=attachments,
                    sender_is_bot=sender_is_bot,
                    mentions=mentions,
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


_CLAUDE_DIR_SUBDIRS = ("agents", "commands", "skills", "hooks")


def _seed_claude_dir(claude_dir: Path) -> None:
    """Create the Claude Code convention skeleton inside a per-agent
    ``.claude/`` dir on worker startup. Idempotent — existing files
    are never overwritten, so users can customise freely.

    The skeleton mirrors Claude Code's project-level layout
    (``agents/``, ``commands/``, ``skills/``, ``hooks/`` as empty
    directories) so all three tool-running adapters (sdk, cli-local,
    cli-docker) find the same structure via their native discovery.
    """
    claude_dir.mkdir(parents=True, exist_ok=True)
    for sub in _CLAUDE_DIR_SUBDIRS:
        (claude_dir / sub).mkdir(exist_ok=True)
