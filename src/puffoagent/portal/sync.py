"""Server-sync loop.

When daemon.yml has a ``server:`` block with a url + user_token, this
task runs alongside the filesystem reconciler. It polls
``GET /api/v4/aiagents?owner=me&include_secrets=true`` every
``sync_interval_seconds`` and reconciles the on-disk agents directory
with the server's list.

The server is the source of truth for *existence*: an agent that
appears on the server is materialised locally; one that vanishes is
archived locally. The ``state`` field (running | paused) from the
server also overwrites the local ``agent.yml`` ``state`` so the webapp
pause/resume buttons propagate within one sync tick.

The *local* filesystem reconciler (daemon.py) still runs — it's what
actually starts/stops workers in response to file changes. This module
just keeps the files in sync with the server.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from pathlib import Path

import aiohttp

from .state import (
    AgentConfig,
    DaemonConfig,
    MattermostConfig,
    RuntimeConfig,
    TriggerRules,
    agent_dir,
    agents_dir,
    archived_dir,
    discover_agents,
    is_valid_agent_id,
)

logger = logging.getLogger(__name__)


async def run_sync_loop(daemon_cfg: DaemonConfig, stop_event: asyncio.Event) -> None:
    """Background task that keeps ``~/.puffoagent/agents/`` in sync with the
    server. Exits when ``stop_event`` is set.
    """
    if not daemon_cfg.has_server_sync():
        logger.info("server sync disabled (no daemon.yml server.url/user_token)")
        return

    logger.info(
        "server sync enabled; url=%s interval=%.0fs",
        daemon_cfg.server.url, daemon_cfg.server.sync_interval_seconds,
    )

    interval = max(5.0, daemon_cfg.server.sync_interval_seconds)
    url = daemon_cfg.server.url.rstrip("/")
    headers = {
        "Authorization": f"Bearer {daemon_cfg.server.user_token}",
        "Content-Type": "application/json",
    }

    async with aiohttp.ClientSession(headers=headers) as session:
        # Immediate tick so the CLI doesn't have to wait the full interval
        # on startup.
        while not stop_event.is_set():
            try:
                await _sync_once(session, url)
            except Exception as exc:
                logger.warning("server sync tick failed: %s", exc)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass


async def _sync_once(session: aiohttp.ClientSession, base_url: str) -> None:
    """One reconciliation pass. Fetch owned agents, diff, write.

    The sync is **scoped to ``base_url``**: a local agent whose
    ``mattermost.url`` doesn't match ``base_url`` is treated as
    belonging to a different server and is left strictly alone —
    not archived (would lose its files), not updated (would
    silently overwrite an unrelated config). This way the
    operator can repoint daemon.yml at a dev server without
    nuking their production agents on disk.
    """
    query = "/api/v4/aiagents?owner=me&include_secrets=true"
    async with session.get(base_url + query) as resp:
        if resp.status != 200:
            body = await resp.text()
            logger.warning("server sync GET /aiagents failed: %s %s", resp.status, body[:200])
            return
        remote_agents = await resp.json()

    remote_by_id: dict[str, dict] = {}
    for remote in remote_agents:
        agent_id = _derive_agent_id(remote)
        if not is_valid_agent_id(agent_id):
            logger.warning("server sync: skipping agent with invalid id %r", agent_id)
            continue
        remote_by_id[agent_id] = remote

    local_ids = set(discover_agents())
    remote_ids = set(remote_by_id.keys())

    # Agents removed on the server → archive locally so the filesystem
    # reconciler cancels the worker on its next tick. Scope: only
    # agents whose ``mattermost.url`` matches the current sync server
    # — leave others on disk untouched.
    for agent_id in local_ids - remote_ids:
        try:
            local = AgentConfig.load(agent_id)
        except Exception:
            continue
        if not _was_created_by_sync(local):
            continue
        if not _url_matches(local.mattermost.url, base_url):
            logger.debug(
                "server sync: skipping archive of %s — belongs to %s, not %s",
                agent_id, local.mattermost.url or "(unset)", base_url,
            )
            continue
        logger.info("server sync: archiving %s (removed server-side)", agent_id)
        _archive_local(agent_id)

    # Agents that exist remotely → create locally or update state.
    # Don't clobber a same-id local agent that belongs to a *different*
    # server — that would silently swap one operator's bot token for
    # another's. Better to log and let the operator rename.
    for agent_id, remote in remote_by_id.items():
        try:
            existing = AgentConfig.load(agent_id)
        except Exception:
            existing = None
        if existing is not None and existing.mattermost.url and not _url_matches(
            existing.mattermost.url, base_url,
        ):
            logger.warning(
                "server sync: skipping %s — local agent already exists "
                "for a different server (%s); rename the local dir to "
                "let the new server's agent install",
                agent_id, existing.mattermost.url,
            )
            continue
        _apply_remote(agent_id, remote)


def _url_matches(a: str, b: str) -> bool:
    """Trailing-slash + scheme-tolerant equality for server URLs.

    The daemon stores the URL as the user typed it; the server
    config sometimes echoes it back with a trailing slash. We
    normalise both before comparing.
    """
    return (a or "").rstrip("/").lower() == (b or "").rstrip("/").lower()


def _derive_agent_id(remote: dict) -> str:
    """Compute a stable local agent id from the server's record.

    Sanitises the display_name down to ASCII alphanumerics + ``-_`` so
    the result can serve as both a filesystem dir name and match
    ``is_valid_agent_id``. Falls back to the bot user id when the name
    has no usable ASCII characters (e.g. "张三" → user id slug).
    """
    raw = remote.get("display_name") or remote.get("profile_name") or ""
    raw = raw.strip().lower()
    allowed = []
    for ch in raw:
        if ch.isascii() and ch.isalnum():
            allowed.append(ch)
        elif ch in " -_":
            allowed.append("-" if ch == " " else ch)
    cleaned = "".join(allowed).strip("-_")
    if cleaned:
        return cleaned[:64]
    # Fall back to the bot user id so CJK-only display names still
    # produce a valid local agent directory.
    return remote.get("user_id", "")[:26]


def _apply_remote(agent_id: str, remote: dict) -> None:
    """Create or update the local agent dir from a server record."""
    directory = agent_dir(agent_id)
    directory.mkdir(parents=True, exist_ok=True)

    # Profile markdown comes from the server when present.
    profile_content = remote.get("profile_content") or ""
    profile_path = directory / "profile.md"
    if profile_content and not _file_equals(profile_path, profile_content):
        profile_path.write_text(profile_content, encoding="utf-8")

    existing: AgentConfig | None = None
    try:
        existing = AgentConfig.load(agent_id)
    except Exception:
        existing = None

    mattermost = MattermostConfig(
        url=remote.get("mattermost_url", ""),
        bot_token=remote.get("bot_token", "") or (existing.mattermost.bot_token if existing else ""),
        team_name=remote.get("team_name", ""),
    )

    # Field ownership split — every field below falls into one of
    # two groups:
    #   - SERVER-OWNED: overwritten from the remote record on every
    #     sync tick (display_name, mattermost, profile.md content).
    #   - LOCALLY-OWNED: read `existing.X if existing else ...` so
    #     sync only seeds an initial value; later edits via the CLI
    #     or a direct agent.yml change are preserved across ticks.
    #     Today: state, runtime, triggers, created_at.
    # Don't add a new "server wins every tick" field here without
    # deliberate discussion — local customisations get clobbered
    # silently and the user has no signal that it happened.
    cfg = AgentConfig(
        id=agent_id,
        # pause/resume on the host would race the next sync tick
        # and flip back to whatever the server last reported if we
        # didn't preserve existing.state. Server-authoritative
        # pause/resume propagation isn't implemented yet — the
        # server's state field is only consulted for brand-new
        # agents.
        state=existing.state if existing else remote.get("state", "running"),
        display_name=remote.get("display_name", agent_id),
        mattermost=mattermost,
        runtime=existing.runtime if existing else RuntimeConfig(),
        profile="profile.md",
        memory_dir="memory",
        workspace_dir="workspace",
        triggers=existing.triggers if existing else TriggerRules(),
        created_at=existing.created_at if existing else int(time.time()),
    )
    if existing is None or _agent_changed(existing, cfg):
        cfg.save()
        _mark_managed_by_sync(directory)
        logger.info("server sync: wrote local agent %s state=%s", agent_id, cfg.state)

    (directory / "memory").mkdir(exist_ok=True)


def _agent_changed(existing: AgentConfig, new: AgentConfig) -> bool:
    return (
        existing.state != new.state
        or existing.mattermost.url != new.mattermost.url
        or existing.mattermost.bot_token != new.mattermost.bot_token
        or existing.mattermost.team_name != new.mattermost.team_name
        or existing.display_name != new.display_name
    )


def _file_equals(path: Path, content: str) -> bool:
    if not path.exists():
        return False
    try:
        return path.read_text(encoding="utf-8") == content
    except OSError:
        return False


MANAGED_MARKER_NAME = ".managed_by_sync"


def _mark_managed_by_sync(directory: Path) -> None:
    (directory / MANAGED_MARKER_NAME).touch()


def _was_created_by_sync(cfg: AgentConfig) -> bool:
    return (agent_dir(cfg.id) / MANAGED_MARKER_NAME).exists()


def _archive_local(agent_id: str) -> None:
    src = agent_dir(agent_id)
    if not src.exists():
        return
    archived_dir().mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = archived_dir() / f"{agent_id}-sync-{stamp}"
    shutil.move(str(src), str(dest))
