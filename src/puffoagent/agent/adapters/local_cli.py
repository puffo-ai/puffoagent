"""Local CLI adapter (dangerous mode).

Spawns a long-lived ``claude`` subprocess on the host with
``--dangerously-skip-permissions`` and stream-json I/O, kept alive
across turns. The agent has the same filesystem and network access
as the user running the daemon — no sandbox, no per-tool prompts.

**Auth.** The claude CLI uses OAuth credentials written by
``claude login`` into ``~/.claude/.credentials.json``. The user must
run ``claude login`` on their machine once before any cli-local agent
can start; we do not inject an ``ANTHROPIC_API_KEY`` and we do not
want to.

**Session persistence.** The subprocess stays running across Mattermost
turns so the CLI's own transcript carries the conversation forward
without us re-sending history. The session id reported by the CLI is
persisted to ``~/.puffoagent/agents/<id>/cli_session.json``; on daemon
restart or crash the adapter spawns a new subprocess with
``--resume <session_id>`` and the conversation picks up where it left
off. See ``cli_session.py``.

This is intentionally the least safe of the three runtime kinds. It
exists for trusted bots on trusted machines and for users who don't
have Docker available. Pick ``cli-docker`` instead if you want
isolation.

A loud one-time WARNING is logged on first turn so operators see it
in the daemon log even if they skipped the README.

Permission-proxy mode (forwarding tool approvals to the Mattermost
owner via an MCP bridge) is tracked separately as a follow-up; see
task #38.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from ...mcp.config import (
    PERMISSION_PROMPT_TOOL,
    default_python_executable,
    mcp_env,
    write_cli_mcp_config,
)
from ...portal.state import seed_claude_home
from .base import Adapter, TurnContext, TurnResult
from .cli_session import AuditLog, ClaudeSession

logger = logging.getLogger(__name__)


class LocalCLIAdapter(Adapter):
    def __init__(
        self,
        agent_id: str,
        model: str,
        workspace_dir: str,
        claude_dir: str,
        session_file: str,
        mcp_config_file: str,
        agent_home_dir: str,
        mattermost_url: str = "",
        mattermost_token: str = "",
        team: str = "",
        owner_username: str = "",
    ):
        self.agent_id = agent_id
        self.model = model
        self.workspace_dir = workspace_dir
        self.claude_dir = claude_dir
        self.session_file = Path(session_file)
        self.mcp_config_file = Path(mcp_config_file)
        # Per-agent virtual $HOME. We point the claude subprocess's
        # HOME / USERPROFILE env at this so its ``~/.claude`` resolves
        # to ``agents/<id>/.claude`` — isolated per agent, no pollution
        # of the operator's personal ~/.claude state.
        self.agent_home_dir = Path(agent_home_dir)
        self.mattermost_url = mattermost_url
        self.mattermost_token = mattermost_token
        self.team = team
        self.owner_username = owner_username
        self._verified = False
        self._session: ClaudeSession | None = None

    async def run_turn(self, ctx: TurnContext) -> TurnResult:
        self._verify()
        session = self._ensure_session()
        user_message = ctx.messages[-1]["content"] if ctx.messages else ""
        return await session.run_turn(user_message, ctx.system_prompt)

    async def warm(self, system_prompt: str) -> None:
        """Spawn the claude subprocess eagerly so the first DM
        doesn't wait for cold start. Only actually spawns if this
        agent has a persisted session — a fresh agent waits for its
        first message to avoid paying for permanently-idle bots.
        """
        self._verify()
        session = self._ensure_session()
        if not session.has_persisted_session():
            logger.info(
                "agent %s: no persisted session; deferring spawn until first message",
                self.agent_id,
            )
            return
        await session.warm(system_prompt)

    async def aclose(self) -> None:
        if self._session is not None:
            await self._session.aclose()
            self._session = None

    def _ensure_session(self) -> ClaudeSession:
        if self._session is not None:
            return self._session
        extra = self._prepare_mcp_args()
        # Point the subprocess at the per-agent virtual home so
        # claude's ``~/.claude`` resolves inside this agent's own
        # dir. Both HOME (POSIX) and USERPROFILE (Node on Windows)
        # need to be set because Claude Code uses Node's
        # ``os.homedir()``.
        env = {
            **os.environ,
            "HOME": str(self.agent_home_dir),
            "USERPROFILE": str(self.agent_home_dir),
        }
        self._session = ClaudeSession(
            agent_id=self.agent_id,
            session_file=self.session_file,
            build_command=self._build_command,
            cwd=self.workspace_dir,
            env=env,
            audit=AuditLog(
                Path(self.workspace_dir) / ".puffoagent" / "audit.log",
                self.agent_id,
            ),
            extra_args=extra,
        )
        return self._session

    def _build_command(self, extra_args: list[str]) -> list[str]:
        # NOTE: do NOT pass --dangerously-skip-permissions. cli-local
        # proxies tool permission decisions to the owner via the MCP
        # permission-prompt callback instead — see
        # ``_prepare_mcp_args``. The callback falls back to "deny on
        # timeout", which is the safer default for a host-level
        # runtime.
        cmd = ["claude"]
        if self.model:
            cmd.extend(["--model", self.model])
        cmd.extend(extra_args)
        return cmd

    def _prepare_mcp_args(self) -> list[str]:
        """Write per-agent MCP config and return the claude-CLI flags
        that register it + enable the permission-prompt proxy."""
        # If we don't have a bot token or URL, skip MCP entirely
        # (we'd be handing claude an un-authable server). The agent
        # still works; it just lacks send_message / upload_file and
        # falls back to default claude permission prompts.
        if not (self.mattermost_url and self.mattermost_token):
            logger.warning(
                "agent %s: cli-local MCP tools unavailable — no mattermost "
                "URL or bot token; send_message / upload_file disabled",
                self.agent_id,
            )
            return []
        env = mcp_env(
            agent_id=self.agent_id,
            url=self.mattermost_url,
            token=self.mattermost_token,
            workspace=self.workspace_dir,
            team=self.team,
            owner_username=self.owner_username,
        )
        write_cli_mcp_config(
            self.mcp_config_file,
            command=default_python_executable(),
            args=["-m", "puffoagent.mcp.puffo_tools"],
            env=env,
        )
        return [
            "--mcp-config", str(self.mcp_config_file),
            "--permission-prompt-tool", PERMISSION_PROMPT_TOOL,
        ]

    def _verify(self) -> None:
        if self._verified:
            return
        if shutil.which("claude") is None:
            raise RuntimeError(
                "claude binary not found on PATH. install the Claude Code CLI "
                "(`npm install -g @anthropic-ai/claude-code`) to use runtime "
                "kind 'cli-local'."
            )
        # Seed this agent's per-agent virtual $HOME from the
        # operator's real $HOME on first use. Isolated per agent —
        # one-time `claude login` on the host covers every cli-local
        # agent. Covers .claude/.credentials.json,
        # .claude/settings.json, and sibling .claude.json.
        host_home = Path.home()
        self.agent_home_dir.mkdir(parents=True, exist_ok=True)
        seeded = seed_claude_home(host_home, self.agent_home_dir)
        if seeded:
            logger.info(
                "agent %s: seeded per-agent virtual $HOME at %s from %s",
                self.agent_id, self.agent_home_dir, host_home,
            )
        agent_claude = self.agent_home_dir / ".claude"
        if not (agent_claude / ".credentials.json").exists():
            logger.warning(
                "agent %s: no .credentials.json in %s (and none at %s). "
                "run `claude login` on the host — first turn will fail "
                "with an auth error otherwise.",
                self.agent_id, agent_claude, host_home / ".claude",
            )
        Path(self.workspace_dir).mkdir(parents=True, exist_ok=True)
        Path(self.claude_dir).mkdir(parents=True, exist_ok=True)

        logger.warning(
            "agent %s: runtime kind 'cli-local' runs claude on the host with "
            "--dangerously-skip-permissions. the agent has your filesystem + "
            "network access with no approval prompts. switch to 'cli-docker' "
            "for sandboxed execution.",
            self.agent_id,
        )
        self._verified = True
