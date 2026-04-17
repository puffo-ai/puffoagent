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
import signal

from .state import (
    AgentConfig,
    DaemonConfig,
    agents_dir,
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

    async def run(self) -> None:
        logger.info("puffoagent portal starting; home=%s", home_dir())
        interval = max(0.5, self.daemon_cfg.reconcile_interval_seconds)

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
                elif _worker_needs_restart(worker.agent_cfg, agent_cfg):
                    logger.info("agent %s: config changed, restarting worker", agent_id)
                    await self._stop_worker(agent_id)
                    worker = Worker(self.daemon_cfg, agent_cfg)
                    self.workers[agent_id] = worker
                    worker.start()
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
