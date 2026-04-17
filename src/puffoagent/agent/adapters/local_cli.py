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
import shutil
from pathlib import Path

from ...mcp.config import (
    PERMISSION_PROMPT_TOOL,
    default_python_executable,
    mcp_env,
    write_cli_mcp_config,
)
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
        self.mattermost_url = mattermost_url
        self.mattermost_token = mattermost_token
        self.team = team
        self.owner_username = owner_username
        self._verified = False
        self._session: ClaudeSession | None = None

    async def run_turn(self, ctx: TurnContext) -> TurnResult:
        self._verify()
        if self._session is None:
            extra = self._prepare_mcp_args()
            self._session = ClaudeSession(
                agent_id=self.agent_id,
                session_file=self.session_file,
                build_command=self._build_command,
                cwd=self.workspace_dir,
                audit=AuditLog(
                    Path(self.workspace_dir) / ".puffoagent" / "audit.log",
                    self.agent_id,
                ),
                extra_args=extra,
            )
        user_message = ctx.messages[-1]["content"] if ctx.messages else ""
        return await self._session.run_turn(user_message, ctx.system_prompt)

    async def aclose(self) -> None:
        if self._session is not None:
            await self._session.aclose()
            self._session = None

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
        creds = Path.home() / ".claude" / ".credentials.json"
        if not creds.exists():
            logger.warning(
                "agent %s: %s not found. run `claude login` on the host "
                "or the first turn will fail with an auth error.",
                self.agent_id, creds,
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
