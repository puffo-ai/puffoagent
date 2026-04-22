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
import json
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
    looks_like_managed_claude_md,
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
    agent_claude_user_dir,
    agent_dir,
    agent_home_dir,
    cli_session_json_path,
    docker_dir,
    docker_shared_dir,
    shared_fs_dir,
)

logger = logging.getLogger(__name__)

RECONNECT_BACKOFF_SECONDS = 5.0

# How often the worker nudges the adapter to check whether OAuth
# credentials are stale and need a refresh ping. The adapter itself
# decides whether to actually run a refresh turn based on the mtime
# of ``.credentials.json`` — this interval is just the poll rate.
CREDENTIAL_REFRESH_TICK_SECONDS = 10 * 60


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
        # The cli-local permission proxy DMs the DAEMON OPERATOR —
        # captured during `puffoagent login` from GET /users/me and
        # persisted to daemon.yml. If it's empty, the MCP callback
        # returns deny on every tool call (claude then refuses to do
        # anything). Surface that misconfig loudly here rather than
        # letting the user debug a silent "agent won't use tools".
        operator = daemon_cfg.server.operator_username
        if not operator:
            logger.warning(
                "agent %s: cli-local permission proxy has no operator "
                "username (daemon.yml server.operator_username is empty). "
                "Run `puffoagent login` again — without it, every tool "
                "approval will auto-deny.",
                agent_cfg.id,
            )
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
            owner_username=operator,
            permission_mode=agent_cfg.runtime.permission_mode,
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

            # Assemble this agent's managed CLAUDE.md from the
            # shared primer + profile + memory snapshot. Written to
            # the USER-level claude dir (<agent_home>/.claude/
            # CLAUDE.md), so Claude Code auto-discovers it as
            # ``$HOME/.claude/CLAUDE.md`` on subprocess spawn. The
            # project-level ``<workspace>/CLAUDE.md`` is deliberately
            # left untouched — that's the agent's own editable layer.
            # SDK/chat-only adapters don't auto-discover, so we also
            # hand the string to PuffoAgent as ``system_prompt``.
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
            write_claude_md(agent_claude_user_dir(agent_id), claude_md)

            # One-time migration: earlier versions wrote the managed
            # CLAUDE.md into ``<workspace>/.claude/CLAUDE.md``. If
            # that file still exists and starts with our managed-
            # content marker, delete it — otherwise Claude Code
            # would see the primer doubled (once user-level, once
            # project-level) and the agent loses the ability to own
            # the project-level layer. We ONLY delete files we
            # recognise as ours.
            old_managed = Path(claude_path) / "CLAUDE.md"
            if looks_like_managed_claude_md(old_managed):
                try:
                    old_managed.unlink()
                    logger.info(
                        "agent %s: migrated stale managed CLAUDE.md out of %s",
                        agent_id, old_managed,
                    )
                except OSError as exc:
                    logger.warning(
                        "agent %s: could not remove stale %s: %s",
                        agent_id, old_managed, exc,
                    )

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

        reload_flag_path = Path(workspace_path) / ".puffoagent" / "reload.flag"
        refresh_flag_path = Path(workspace_path) / ".puffoagent" / "refresh.flag"
        # Per-turn context file for the cli-local permission hook. The
        # hook is a separate subprocess claude spawns per tool call,
        # so it can't reach into the worker's memory — it reads this
        # file instead. Contains the channel + root the permission
        # request should reply in so the DM-like prompt lands in the
        # same thread as the user's original message.
        current_turn_path = Path(workspace_path) / ".puffoagent" / "current_turn.json"

        async def on_message(
            channel_id, channel_name, sender, sender_email, text,
            root_id, direct, attachments, sender_is_bot, mentions,
            post_id, create_at, followups,
        ):
            # Agent can drop a reload flag via the reload_system_prompt
            # MCP tool. Honour it BEFORE the turn so the first message
            # after the flag-drop picks up fresh CLAUDE.md / profile /
            # memory content. Flag-drop happens DURING a turn so we
            # can't act within that turn — the next one is the earliest
            # we can reload.
            if reload_flag_path.exists():
                await _reload_from_disk(
                    agent_id=agent_id,
                    shared_path=shared_path,
                    profile_path=profile_path,
                    memory_path=memory_path,
                    workspace_path=workspace_path,
                    puffo=puffo,
                    adapter=self._adapter,
                    flag_path=reload_flag_path,
                )
                # Reload already subsumes refresh (killed subprocess +
                # reread config). Drop any sibling refresh flag so we
                # don't double-restart.
                try:
                    refresh_flag_path.unlink()
                except OSError:
                    pass
            elif refresh_flag_path.exists():
                await _refresh_from_disk(
                    agent_id=agent_id,
                    adapter=self._adapter,
                    flag_path=refresh_flag_path,
                )
            try:
                current_turn_path.parent.mkdir(parents=True, exist_ok=True)
                current_turn_path.write_text(
                    json.dumps({
                        "channel_id": channel_id,
                        "root_id": root_id,
                        "triggering_post_id": post_id,
                    }),
                    encoding="utf-8",
                )
            except OSError as exc:
                logger.warning(
                    "agent %s: could not write current_turn.json: %s "
                    "(permission hook will fail-open)", agent_id, exc,
                )
            typing_task = asyncio.ensure_future(_keep_typing(client, channel_id, root_id))
            try:
                reply = await puffo.handle_message(
                    channel_id, channel_name, sender, sender_email, text, direct,
                    attachments=attachments,
                    sender_is_bot=sender_is_bot,
                    mentions=mentions,
                    post_id=post_id,
                    root_id=root_id,
                    create_at=create_at,
                    followups=followups,
                )
            except Exception as exc:
                logger.error("agent %s: handle_message error: %s", agent_id, exc, exc_info=True)
                reply = None
            finally:
                typing_task.cancel()
                # Clear the turn context so any proactive/background
                # agent work that happens after the turn ends doesn't
                # inherit a stale channel/root. The hook fails-open
                # when the file is absent.
                try:
                    current_turn_path.unlink()
                except OSError:
                    pass
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

        async def credential_refresh():
            """Periodically nudge the adapter to refresh its OAuth
            credentials before they expire. Idle agents would
            otherwise let the access token lapse; the adapter's
            own mtime check keeps this cheap for agents whose
            shared credentials file was refreshed elsewhere.
            """
            # First tick: wait one interval so we don't pile onto
            # the worker's warm() with a refresh at startup.
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=CREDENTIAL_REFRESH_TICK_SECONDS,
                )
                return
            except asyncio.TimeoutError:
                pass
            while not self._stop.is_set():
                try:
                    await self._adapter.refresh_ping()
                except Exception as exc:
                    logger.warning(
                        "agent %s: credential refresh tick failed: %s",
                        agent_id, exc,
                    )
                # Reflect the smoke-test outcome into runtime.health
                # so operators can see auth_failed via ``puffoagent
                # status`` without tailing logs. ``auth_healthy`` is
                # None until the first probe runs — map that to
                # "unknown" so we never overwrite a real result with
                # a stale default.
                probed = getattr(self._adapter, "auth_healthy", None)
                if probed is True:
                    self.runtime.health = "ok"
                elif probed is False:
                    self.runtime.health = "auth_failed"
                self.runtime.save(agent_id)
                try:
                    await asyncio.wait_for(
                        self._stop.wait(),
                        timeout=CREDENTIAL_REFRESH_TICK_SECONDS,
                    )
                except asyncio.TimeoutError:
                    pass

        hb_task = asyncio.ensure_future(heartbeat())
        refresh_task = asyncio.ensure_future(credential_refresh())
        try:
            while not self._stop.is_set():
                try:
                    await client.listen(on_message=on_message)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "agent %s: listen() crashed: %s: %s — reconnecting in %.1fs",
                        agent_id, type(exc).__name__, exc, RECONNECT_BACKOFF_SECONDS,
                    )
                    self.runtime.error = f"{type(exc).__name__}: {exc}"
                    self.runtime.save(agent_id)
                if self._stop.is_set():
                    break
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=RECONNECT_BACKOFF_SECONDS)
                except asyncio.TimeoutError:
                    pass
        finally:
            hb_task.cancel()
            refresh_task.cancel()
            for task in (hb_task, refresh_task):
                try:
                    await task
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


async def _reload_from_disk(
    *,
    agent_id: str,
    shared_path: Path,
    profile_path: str,
    memory_path: str,
    workspace_path: str,
    puffo,
    adapter,
    flag_path: Path,
) -> None:
    """Rebuild the agent's managed CLAUDE.md from the current disk
    state (shared primer + profile.md + memory snapshot), update the
    shell's cached ``system_prompt``, then ask the adapter to drop any
    cached subprocess so the next turn re-reads CLAUDE.md.

    Called by ``on_message`` when the agent has set the reload flag
    via the ``reload_system_prompt`` MCP tool. Failures are logged
    but don't prevent the turn — a stale prompt is strictly better
    than a dropped message.
    """
    try:
        ensure_shared_primer(shared_path)
        sync_shared_skills(shared_path, Path(workspace_path))
        primer = read_shared_primer(shared_path)
        try:
            profile_text = Path(profile_path).read_text(encoding="utf-8")
        except OSError:
            profile_text = ""
        new_md = assemble_claude_md(
            shared_primer=primer,
            profile=profile_text,
            memory_snapshot=read_memory_snapshot(Path(memory_path)),
        )
        write_claude_md(agent_claude_user_dir(agent_id), new_md)
        puffo.system_prompt = new_md
        await adapter.reload(new_md)
        logger.info("agent %s: reloaded system prompt from disk", agent_id)
    except Exception as exc:
        logger.warning("agent %s: reload failed: %s", agent_id, exc)
    finally:
        try:
            flag_path.unlink()
        except OSError:
            pass


async def _refresh_from_disk(
    *,
    agent_id: str,
    adapter,
    flag_path: Path,
) -> None:
    """Kill the claude subprocess so it respawns on the next turn and
    re-reads skills, .mcp.json, .claude.json — without rebuilding
    CLAUDE.md. Honour an optional ``model`` override in the flag
    payload so the respawn picks up the new model flag.

    Called by ``on_message`` when the agent has set the refresh flag
    via the ``refresh`` MCP tool. Much cheaper than ``_reload_from_disk``
    because it skips shared-primer / profile / memory assembly — use
    this path for pure config churn (new skills, new MCPs, model
    switch), use reload for CLAUDE.md edits.
    """
    try:
        try:
            raw = flag_path.read_text(encoding="utf-8")
            payload = json.loads(raw) if raw.strip() else {}
        except (OSError, ValueError):
            payload = {}
        new_model = payload.get("model") if isinstance(payload, dict) else None
        if new_model is not None and hasattr(adapter, "model"):
            old_model = getattr(adapter, "model", "")
            adapter.model = str(new_model)
            logger.info(
                "agent %s: model override via refresh: %r -> %r",
                agent_id, old_model, adapter.model,
            )
        # reload() ignores its argument — both adapter implementations
        # just tear down the subprocess. The next turn spawns fresh
        # and re-reads all on-disk config (skills, mcpServers, model).
        await adapter.reload("")
        logger.info(
            "agent %s: refreshed (subprocess will respawn next turn)",
            agent_id,
        )
    except Exception as exc:
        logger.warning("agent %s: refresh failed: %s", agent_id, exc)
    finally:
        try:
            flag_path.unlink()
        except OSError:
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
