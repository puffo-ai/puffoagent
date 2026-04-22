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

import asyncio
import json
import logging
import os
import shutil
import time
from pathlib import Path

from ...mcp.config import (
    default_python_executable,
    mcp_env,
    write_cli_mcp_config,
)
from ...portal.state import (
    link_host_credentials,
    seed_claude_home,
    sync_host_mcp_servers,
    sync_host_skills,
)
from .base import Adapter, TurnContext, TurnResult, looks_like_auth_failure
from .cli_session import AuditLog, ClaudeSession

logger = logging.getLogger(__name__)


# Timeout for the refresh one-shot. See docker_cli for the rationale.
REFRESH_ONESHOT_TIMEOUT_SECONDS = 120

# How many seconds the permission proxy hook will wait for an owner
# reply before denying. Exposed via PUFFO_PERMISSION_TIMEOUT env var
# the hook reads on startup.
PERMISSION_HOOK_TIMEOUT_SECONDS = 300

# Claude Code built-in tools that default mode would normally prompt
# on. Our PreToolUse hook intercepts these; reads (Read/Glob/Grep)
# and MCP tools pass through unsurveyed (reads auto-approve in
# default mode, MCP tools are the agent's talking-to-Mattermost path
# and shouldn't fire per-call DMs).
PERMISSION_HOOK_FULL_MATCHER = "Bash|Edit|Write|MultiEdit|NotebookEdit|WebFetch|WebSearch"

# Narrower matcher for ``acceptEdits`` mode: the user has told
# claude to auto-accept file edits, so we exclude the edit tools
# from the proxy and only DM on shell + network. If we kept the
# full matcher here, setting ``acceptEdits`` would do nothing
# visible — the DM would still fire on every Edit/Write.
PERMISSION_HOOK_NON_EDIT_MATCHER = "Bash|WebFetch|WebSearch"

# Substring we look for in existing hook commands to identify
# puffoagent-registered entries during cleanup. Matching on the
# module path (rather than the matcher string) means we find our
# own prior entries across mode switches — switching permission_mode
# from ``default`` → ``acceptEdits`` would otherwise leak a stale
# full-matcher entry that shadows the new narrow one.
_HOOK_COMMAND_MARKER = "puffoagent.hooks.permission"


# Claude Code permission modes we pass through to ``--permission-mode``.
# Excludes ``plan`` (read-only research mode, not useful for chat-reply
# agents). ``default`` routes everything non-read through our MCP
# permission-prompt callback, which is what most cli-local agents
# should use; ``bypassPermissions`` disables the proxy entirely.
# See https://code.claude.com/docs/en/permission-modes.
VALID_PERMISSION_MODES = frozenset({
    "default",
    "acceptEdits",
    "auto",
    "dontAsk",
    "bypassPermissions",
})


def _is_puffoagent_hook_entry(entry: object) -> bool:
    """True if ``entry`` is a PreToolUse hook this adapter previously
    wrote — identified by the ``puffoagent.hooks.permission`` marker
    in its command. Used to find and remove our own stale entries
    during reconciliation without trampling hooks the user or agent
    added themselves.
    """
    if not isinstance(entry, dict):
        return False
    hooks = entry.get("hooks") or []
    return any(
        isinstance(h, dict) and _HOOK_COMMAND_MARKER in (h.get("command") or "")
        for h in hooks
    )


def _sanitise_permission_mode(mode: str, agent_id: str) -> str:
    """Return a validated permission mode, falling back to 'default'
    with a WARNING if the caller supplied something claude doesn't
    recognise. Never raises — a bad config value shouldn't kill the
    worker, but it shouldn't silently look like the user asked for
    something different either.
    """
    if not mode:
        return "default"
    if mode in VALID_PERMISSION_MODES:
        return mode
    logger.warning(
        "agent %s: unknown permission_mode %r — falling back to "
        "'default'. valid: %s",
        agent_id, mode, sorted(VALID_PERMISSION_MODES),
    )
    return "default"


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
        permission_mode: str = "default",
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
        self.permission_mode = _sanitise_permission_mode(permission_mode, agent_id)
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

    async def reload(self, new_system_prompt: str) -> None:
        """Close the long-lived claude subprocess so the next
        ``run_turn`` spawns a fresh one that re-reads CLAUDE.md.
        """
        if self._session is not None:
            await self._session.aclose()
            self._session = None

    def _credentials_expires_in_seconds(self) -> int | None:
        # cli-local agents share the HOST's ``.credentials.json`` via
        # ``link_host_credentials`` — symlink where permitted, periodic
        # copy fallback elsewhere. The expiry check always targets the
        # host file so agents see live operator state, matching the
        # cli-docker bind-mount model. Parses ``expiresAt`` directly
        # rather than relying on mtime (mtime only advances when the
        # file is REWRITTEN, not when the token is still valid).
        #
        # The link_host_credentials call here doubles as the periodic
        # re-sync for copy-mode agents — called once per refresh_ping
        # tick, cheap no-op when symlinked.
        link_host_credentials(Path.home(), self.agent_home_dir)
        host_credentials = Path.home() / ".claude" / ".credentials.json"
        try:
            data = json.loads(host_credentials.read_text(encoding="utf-8"))
            expires_ms = int(data["claudeAiOauth"]["expiresAt"])
        except (OSError, ValueError, KeyError, TypeError):
            return None
        return int(expires_ms / 1000 - time.time())

    async def _run_refresh_oneshot(self) -> None:
        """Spawn a short-lived ``claude --print ...`` host subprocess
        with the per-agent ``HOME`` env. Same rationale as
        DockerCLIAdapter: the long-lived stream-json session
        refreshes tokens in memory but doesn't write
        ``.credentials.json`` back out; a separate one-shot process
        forces the credentials-write path on exit.
        """
        self._verify()
        env = {
            **os.environ,
            "HOME": str(self.agent_home_dir),
            "USERPROFILE": str(self.agent_home_dir),
        }
        # --dangerously-skip-permissions is required here even when
        # the agent's long-lived session runs on ``default``. In
        # --print mode claude can't surface permission prompts, so
        # without a bypass it exits before the API call (rc=1,
        # iterations:[], input_tokens:0) and no refresh happens.
        # The docker_cli refresh uses the same flag for the same
        # reason. Scope is one round-trip with an "ok" prompt — no
        # tool calls expected.
        cmd = [
            "claude", "--dangerously-skip-permissions",
            "--print", "--max-turns", "1",
            "--output-format", "stream-json", "--verbose",
        ]
        if self.model:
            cmd.extend(["--model", self.model])
        cmd.append("ok")
        started_at = time.time()
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=self.workspace_dir,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=REFRESH_ONESHOT_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "agent %s: refresh one-shot timed out after %ds",
                self.agent_id, REFRESH_ONESHOT_TIMEOUT_SECONDS,
            )
            return
        except FileNotFoundError:
            logger.warning(
                "agent %s: refresh one-shot: claude binary missing",
                self.agent_id,
            )
            return
        elapsed = time.time() - started_at
        out_text = stdout.decode("utf-8", errors="replace")
        err_text = stderr.decode("utf-8", errors="replace")
        # The refresh one-shot doubles as a real inference smoke
        # test. ``claude auth status`` reporting logged-in isn't
        # enough — during the 2026-04-21 incident, status said OK
        # while every API call got 401. Pattern-match the output
        # to flip auth_healthy; the worker reads that for status
        # reporting and to stop the bot from producing noisy
        # replies while the operator re-auths.
        if looks_like_auth_failure(out_text, err_text):
            logger.error(
                "agent %s: refresh one-shot hit an auth failure "
                "(rc=%d in %.1fs). operator re-auth likely required. "
                "stdout: %s | stderr: %s",
                self.agent_id, proc.returncode, elapsed,
                out_text.strip()[-400:], err_text.strip()[-400:],
            )
            self.auth_healthy = False
        elif proc.returncode != 0:
            logger.warning(
                "agent %s: refresh one-shot rc=%d in %.1fs | "
                "stdout: %s | stderr: %s",
                self.agent_id, proc.returncode, elapsed,
                out_text.strip()[-400:], err_text.strip()[-400:],
            )
        else:
            logger.debug(
                "agent %s: refresh one-shot rc=0 in %.1fs",
                self.agent_id, elapsed,
            )
            self.auth_healthy = True

    async def aclose(self) -> None:
        if self._session is not None:
            await self._session.aclose()
            self._session = None

    def _ensure_session(self) -> ClaudeSession:
        if self._session is not None:
            return self._session
        extra = self._prepare_mcp_args()
        # Register the PreToolUse permission hook before spawning.
        # settings.json is read fresh on every claude subprocess
        # start, so this is idempotent-on-every-worker-restart.
        self._write_permission_hook_settings()
        # Point the subprocess at the per-agent virtual home so
        # claude's ``~/.claude`` resolves inside this agent's own
        # dir. Both HOME (POSIX) and USERPROFILE (Node on Windows)
        # need to be set because Claude Code uses Node's
        # ``os.homedir()``. The PUFFO_* vars are consumed by the
        # hook subprocess claude spawns per tool call.
        env = {
            **os.environ,
            "HOME": str(self.agent_home_dir),
            "USERPROFILE": str(self.agent_home_dir),
            **self._permission_hook_env(),
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

    def _permission_hook_env(self) -> dict[str, str]:
        """Env vars the PreToolUse hook script reads. Claude
        inherits the parent's env and passes it to hook subprocesses,
        so setting these on the claude spawn reaches the hook
        without any other plumbing.
        """
        return {
            "PUFFO_URL": self.mattermost_url,
            "PUFFO_BOT_TOKEN": self.mattermost_token,
            "PUFFO_OPERATOR_USERNAME": self.owner_username,
            "PUFFO_AGENT_ID": self.agent_id,
            "PUFFO_PERMISSION_TIMEOUT": str(PERMISSION_HOOK_TIMEOUT_SECONDS),
        }

    def _hook_matcher_for_mode(self) -> str | None:
        """Return the PreToolUse hook matcher for this agent's
        ``permission_mode``, or ``None`` when the mode opts out of
        proxying entirely.

        Claude Code runs PreToolUse hooks on every matching tool
        call regardless of ``--permission-mode``, so if we kept the
        hook registered for every mode the user's choice would be
        a no-op — ``bypassPermissions`` would still DM the owner on
        every Bash call. Mapping the matcher to the mode is the
        only honest way to make the CLI flag mean what it says:

          - ``default``          → full matcher (proxy everything)
          - ``acceptEdits``      → shell + network only (edits skip the proxy)
          - ``auto``/``dontAsk``/``bypassPermissions`` → no hook
        """
        mode = self.permission_mode
        if mode == "default":
            return PERMISSION_HOOK_FULL_MATCHER
        if mode == "acceptEdits":
            return PERMISSION_HOOK_NON_EDIT_MATCHER
        return None

    def _write_permission_hook_settings(self) -> None:
        """Reconcile the agent's project-level ``settings.json`` so the
        PreToolUse puffoagent hook matches this agent's
        ``permission_mode``.

        Written to the project level (``workspace/.claude/``), not
        user level (``agent_home_dir/.claude/``), so this file can
        be overwritten wholesale without disturbing settings that
        were seeded from the host. Existing non-puffoagent hooks
        are preserved — only the puffoagent entry is added, updated,
        or removed.

        Uses ``default_python_executable()`` as the hook's python
        so the hook runs under the same interpreter that has
        puffoagent installed.
        """
        settings_path = Path(self.claude_dir) / "settings.json"
        settings_path.parent.mkdir(parents=True, exist_ok=True)

        # Merge into existing content if present — keeps any hooks
        # the agent set for itself (via MCP reload_system_prompt,
        # or a hand-edit) while still registering ours.
        try:
            existing = json.loads(settings_path.read_text(encoding="utf-8"))
            if not isinstance(existing, dict):
                existing = {}
        except (FileNotFoundError, ValueError, OSError):
            existing = {}

        hooks_cfg = existing.get("hooks") or {}
        pretool = hooks_cfg.get("PreToolUse") or []
        # Drop any previous puffoagent entry, keyed by the command
        # signature so we catch entries across matcher changes
        # (e.g. switching permission_mode from default → acceptEdits).
        pretool = [
            entry for entry in pretool
            if not _is_puffoagent_hook_entry(entry)
        ]

        matcher = self._hook_matcher_for_mode()
        if matcher is not None:
            pretool.append({
                "matcher": matcher,
                "hooks": [{
                    "type": "command",
                    "command": (
                        f'"{default_python_executable()}" '
                        f"-m puffoagent.hooks.permission"
                    ),
                    "timeout": PERMISSION_HOOK_TIMEOUT_SECONDS + 60,
                }],
            })

        if pretool:
            hooks_cfg["PreToolUse"] = pretool
        elif "PreToolUse" in hooks_cfg:
            del hooks_cfg["PreToolUse"]
        if hooks_cfg:
            existing["hooks"] = hooks_cfg
        elif "hooks" in existing:
            del existing["hooks"]

        tmp = settings_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(existing, indent=2), encoding="utf-8")
        tmp.replace(settings_path)

    def _build_command(self, extra_args: list[str]) -> list[str]:
        # We pass ``--permission-mode`` rather than
        # ``--dangerously-skip-permissions`` so the user controls
        # which tool categories auto-approve. Anything claude would
        # normally prompt on still flows through the MCP
        # permission-prompt callback in ``_prepare_mcp_args`` — that
        # callback falls back to "deny on timeout", which is the
        # safer default for a host-level runtime.
        cmd = ["claude", "--permission-mode", self.permission_mode]
        if self.model:
            cmd.extend(["--model", self.model])
        cmd.extend(extra_args)
        return cmd

    def _prepare_mcp_args(self) -> list[str]:
        """Write per-agent MCP config and return the claude-CLI flag
        that registers it.

        Permission proxying lives in a PreToolUse hook (see
        ``_write_permission_hook_settings``), NOT in the MCP
        ``--permission-prompt-tool`` flag. The flag is documented as
        non-interactive-mode-only, and cli-local runs claude in
        interactive stream-json mode where the flag is silently
        ignored. The hook works in every mode.
        """
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
            runtime_kind="cli-local",
        )
        write_cli_mcp_config(
            self.mcp_config_file,
            command=default_python_executable(),
            args=["-m", "puffoagent.mcp.puffo_tools"],
            env=env,
        )
        return ["--mcp-config", str(self.mcp_config_file)]

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
        # covers .claude/settings.json and sibling .claude.json.
        # Credentials are handled separately (link_host_credentials
        # below) so every agent tracks the operator's live OAuth
        # state instead of diverging over time.
        host_home = Path.home()
        self.agent_home_dir.mkdir(parents=True, exist_ok=True)
        seeded = seed_claude_home(host_home, self.agent_home_dir)
        if seeded:
            logger.info(
                "agent %s: seeded per-agent virtual $HOME at %s from %s",
                self.agent_id, self.agent_home_dir, host_home,
            )
        # Shared credentials: point agent's .credentials.json at the
        # host's live file so `claude login` on the host (or any
        # refresh by any cli-local agent) is visible to every agent
        # on the next read. Symlink preferred; falls back to a copy
        # on systems where symlinks aren't permitted (re-synced on
        # every refresh_ping tick via _credentials_expires_in_seconds).
        mode = link_host_credentials(host_home, self.agent_home_dir)
        logger.info(
            "agent %s: shared host credentials (%s)",
            self.agent_id, mode,
        )
        # One-way sync of host-installed user-level skills and MCP
        # registrations into the per-agent virtual $HOME. Runs every
        # worker start so host edits propagate without recreating
        # the agent. The unreachable-command list returned by
        # sync_host_mcp_servers is deliberately ignored here: on
        # cli-local the agent subprocess runs on the host, so
        # absolute paths like ``/Users/…`` / ``C:\…`` do resolve.
        # (docker_cli.py warns on them; local_cli has no such
        # concern.)
        skill_count = sync_host_skills(host_home, self.agent_home_dir)
        if skill_count:
            logger.info(
                "agent %s: synced %d host skill(s) into %s",
                self.agent_id, skill_count,
                self.agent_home_dir / ".claude" / "skills",
            )
        merged_mcp, _ = sync_host_mcp_servers(host_home, self.agent_home_dir)
        if merged_mcp:
            logger.info(
                "agent %s: merged %d host MCP server registration(s) "
                "into per-agent .claude.json", self.agent_id, merged_mcp,
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
