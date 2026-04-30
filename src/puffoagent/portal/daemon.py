"""The multi-agent reconciler.

The daemon walks ``~/.puffoagent/agents/`` every ``reconcile_interval_seconds``
and diffs the on-disk state against its in-memory task registry. New
agent directories become Workers; directories that disappear or change
their ``state`` field get stopped. This mirror-the-filesystem model is
how the CLI controls the daemon without any IPC.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import time

from .state import (
    AgentConfig,
    DaemonConfig,
    agent_dir,
    agents_dir,
    archive_flag_path,
    archived_dir,
    clear_daemon_pid,
    discover_agents,
    home_dir,
    is_daemon_alive,
    read_daemon_pid,
    write_daemon_pid,
)
from .sync import run_sync_loop
from .worker import Worker

logger = logging.getLogger(__name__)


class Daemon:
    def __init__(self, daemon_cfg: DaemonConfig):
        self.daemon_cfg = daemon_cfg
        self.workers: dict[str, Worker] = {}
        self._stop = asyncio.Event()
        # Cap on per-worker warm wait. A claude resume on a long
        # session takes seconds, not minutes; 120s is the upper bound
        # past which we give up on this agent and let the next one
        # start (the wedged worker keeps trying in the background).
        self._warm_serialise_timeout = 120.0

    async def run(self) -> None:
        logger.info("puffoagent portal starting; home=%s", home_dir())
        interval = max(0.5, self.daemon_cfg.reconcile_interval_seconds)

        # Fire a one-shot version check so the operator sees an
        # actionable warning at startup if their daemon is older than
        # the latest GitHub release. Runs in a worker thread (urllib
        # is blocking) and never blocks startup. Skipped silently
        # for source installs — `pip install -e .` users are
        # almost always *ahead* of the latest tag.
        asyncio.ensure_future(_log_outdated_version_warning())

        # The server-sync loop (server → filesystem) and the local
        # reconciler (filesystem → workers) run in parallel. Both stop
        # when ``self._stop`` is set.
        sync_task = asyncio.ensure_future(run_sync_loop(self.daemon_cfg, self._stop))

        try:
            while not self._stop.is_set():
                try:
                    await self._reconcile_once()
                except Exception as exc:
                    logger.error("reconcile tick crashed: %s", exc, exc_info=True)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=interval)
                except asyncio.TimeoutError:
                    pass
        finally:
            sync_task.cancel()
            try:
                await sync_task
            except (asyncio.CancelledError, Exception):
                pass
            await self._stop_all_workers()
            clear_daemon_pid()
            logger.info("puffoagent portal stopped")

    def request_stop(self) -> None:
        self._stop.set()

    async def _reconcile_once(self) -> None:
        on_disk = set(discover_agents())
        running = set(self.workers.keys())

        # Agents that disappeared from disk → stop.
        for agent_id in running - on_disk:
            logger.info("agent %s: directory removed, stopping worker", agent_id)
            await self._stop_worker(agent_id)

        # Agents whose worker asked to be archived (e.g., because
        # their Puffo space was deleted server-side and the
        # mattermost websocket fired ``delete_team``). Stop the
        # worker, move the dir to ``archived/``, drop out of the
        # reconcile loop for this agent — subsequent iterations
        # will just not see it on disk.
        archived_this_tick: set[str] = set()
        for agent_id in sorted(on_disk):
            if archive_flag_path(agent_id).exists():
                await self._archive_on_flag(agent_id)
                archived_this_tick.add(agent_id)
        on_disk -= archived_this_tick

        # Agents on disk → check state and (start | stop | leave alone).
        for agent_id in sorted(on_disk):
            try:
                agent_cfg = AgentConfig.load(agent_id)
            except Exception as exc:
                logger.warning("agent %s: failed to load agent.yml: %s", agent_id, exc)
                continue

            desired_state = agent_cfg.state
            worker = self.workers.get(agent_id)

            if desired_state == "running":
                if worker is None:
                    logger.info("agent %s: starting worker", agent_id)
                    worker = Worker(self.daemon_cfg, agent_cfg)
                    self.workers[agent_id] = worker
                    worker.start()
                    # Serialise heavy startup. ``adapter.warm()`` reads
                    # the persisted session file into Node's heap;
                    # spawning N agents in parallel piles N copies of
                    # the transcript into memory at once and OOMs the
                    # box. Awaiting per-worker keeps peak RSS tied to
                    # one agent at a time. Capped so a wedged warm
                    # can't pin the whole reconciler.
                    await worker.wait_warm(timeout=self._warm_serialise_timeout)
                elif _worker_needs_restart(worker.agent_cfg, agent_cfg):
                    logger.info("agent %s: config changed, restarting worker", agent_id)
                    await self._stop_worker(agent_id)
                    worker = Worker(self.daemon_cfg, agent_cfg)
                    self.workers[agent_id] = worker
                    worker.start()
                    await worker.wait_warm(timeout=self._warm_serialise_timeout)
                else:
                    worker.agent_cfg = agent_cfg
            elif desired_state == "paused":
                if worker is not None:
                    logger.info("agent %s: state=paused, stopping worker", agent_id)
                    await self._stop_worker(agent_id)
            else:
                logger.warning("agent %s: unknown state %r", agent_id, desired_state)

    async def _stop_worker(self, agent_id: str) -> None:
        worker = self.workers.pop(agent_id, None)
        if worker is not None:
            await worker.stop()

    async def _stop_all_workers(self) -> None:
        ids = list(self.workers.keys())
        await asyncio.gather(*(self._stop_worker(i) for i in ids), return_exceptions=True)

    async def _archive_on_flag(self, agent_id: str) -> None:
        """Worker dropped an ``archive.flag`` sentinel (server-side
        space deletion, etc.). Stop the worker and move the agent
        dir to ``archived/<id>-ws-<stamp>/``. The ``-ws-`` suffix
        distinguishes this from operator-initiated archives (no
        suffix) and sync-driven archives (``-sync-``)."""
        logger.warning(
            "agent %s: archive.flag detected, stopping worker + archiving",
            agent_id,
        )
        await self._stop_worker(agent_id)
        src = agent_dir(agent_id)
        if not src.exists():
            return
        archived_dir().mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest = archived_dir() / f"{agent_id}-ws-{stamp}"
        try:
            shutil.move(str(src), str(dest))
            logger.info("agent %s: archived to %s", agent_id, dest)
        except OSError as exc:
            logger.error(
                "agent %s: archive failed: %s (flag still present — will retry next tick)",
                agent_id, exc,
            )


async def _log_outdated_version_warning() -> None:
    """Background task: compare local puffoagent version against the
    latest GitHub release and log a WARNING if behind. Best-effort —
    network errors / missing metadata silently skip the check.
    """
    # Lazy import to avoid the cli ↔ daemon module cycle at load time.
    from .cli import (
        fetch_latest_release_tag,
        get_local_version,
        is_outdated,
        is_source_install,
        upgrade_command_for_install_mode,
    )
    if is_source_install():
        # Source / editable installs may legitimately be ahead of
        # the latest release; warning them is noise.
        return
    try:
        local = get_local_version()
        remote = await asyncio.to_thread(fetch_latest_release_tag)
    except Exception:
        return
    if not remote:
        return
    if is_outdated(local, remote):
        logger.warning(
            "puffoagent %s is behind the latest release (%s). "
            "this daemon may be missing features or fixes documented "
            "on github. to upgrade: %s",
            local, remote, upgrade_command_for_install_mode(),
        )
    else:
        logger.info(
            "puffoagent %s (latest release: %s)", local, remote,
        )


def _worker_needs_restart(old, new) -> bool:
    """Return True when fields that live inside the worker's in-memory
    state changed: the Mattermost WebSocket credentials, the profile
    path, or anything about the adapter runtime (kind / provider /
    model / api_key).
    """
    return (
        old.mattermost.url != new.mattermost.url
        or old.mattermost.bot_token != new.mattermost.bot_token
        or old.profile != new.profile
        or old.runtime != new.runtime
    )


async def run_daemon() -> int:
    # Prevent two daemons from fighting over the same agents.
    if is_daemon_alive():
        pid = read_daemon_pid()
        logger.error("another daemon is already running (pid=%s)", pid)
        return 1

    home_dir().mkdir(parents=True, exist_ok=True)
    agents_dir().mkdir(parents=True, exist_ok=True)

    daemon_cfg = DaemonConfig.load()
    write_daemon_pid(os.getpid())

    daemon = Daemon(daemon_cfg)

    loop = asyncio.get_running_loop()

    def handle_signal() -> None:
        logger.info("received stop signal; shutting down")
        daemon.request_stop()

    # Best-effort: SIGINT and SIGTERM on posix; Ctrl+C on windows.
    for sig_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, handle_signal)
        except NotImplementedError:
            # Windows proactor loop doesn't support add_signal_handler for SIGTERM.
            pass

    try:
        await daemon.run()
    except KeyboardInterrupt:
        daemon.request_stop()
        await daemon.run()
    return 0
