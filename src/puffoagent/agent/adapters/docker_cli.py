"""Docker CLI adapter.

Runs the Claude Code CLI inside a per-agent Docker container. The
container is the sandbox — Claude Code runs with
``--dangerously-skip-permissions`` inside, because escape from the
container back to the host is non-trivial and the user opted into
this model by picking ``kind=cli-docker``.

**Auth.** Each agent gets its own isolated claude identity. The
container's ``/home/agent`` is bind-mounted from
``~/.puffoagent/agents/<id>/`` on the host, so the CLI's
``~/.claude/`` inside the container resolves to
``~/.puffoagent/agents/<id>/.claude/`` on the host — a per-agent
dir, seeded once from the operator's real ``~/.claude`` (credentials
+ settings only). Bot sessions, history, and token refreshes stay
inside that dir — no bleed between agents, no bleed back to the
operator's personal claude state. No ``ANTHROPIC_API_KEY`` is
injected.

**Cross-agent coordination.** A second bind-mount exposes
``~/.puffoagent/shared/`` at ``/workspace/.shared`` inside the
container so all agents on this host can drop files, read each
other's artifacts, and cooperate at the filesystem level.

**Lifecycle layering.**
    container   — one per agent, ``puffo-<id>``, started lazily,
                  torn down on ``aclose()``.
    claude      — one long-lived ``claude --input-format stream-json
                  --output-format stream-json`` subprocess *inside*
                  the container, started on first turn via
                  ``docker exec -i``, kept alive across turns. Handled
                  by ``ClaudeSession``.
    session id  — persisted on the host at
                  ``~/.puffoagent/agents/<id>/cli_session.json`` so a
                  daemon restart (or a container restart) re-spawns
                  with ``--resume <id>`` and the transcript carries
                  forward.

Image: bundled inline as a Dockerfile string, built on first use via
``docker build -t puffo/agent-runtime:latest -`` (stdin). Users who
want a custom image set ``runtime.docker_image`` to a pre-built tag
and puffoagent skips the build step.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

from ...mcp.config import (
    export_mcp_script,
    mcp_env,
    write_cli_mcp_config,
)
from ...portal.state import seed_claude_home
from .base import Adapter, TurnContext, TurnResult
from .cli_session import AuditLog, ClaudeSession


logger = logging.getLogger(__name__)


# Bump this when the Dockerfile changes so existing hosts pick up
# a rebuild without the user having to remember to prune the old
# image tag. ``_ensure_image`` only builds when the tag is missing
# locally, so a stable tag would mask Dockerfile edits.
DEFAULT_IMAGE = "puffo/agent-runtime:v5"

# Kept minimal: node (for the claude CLI npm package), git, the tools
# claude's built-in commands shell out to. No COPY/ADD so the build
# context is empty and we can build from stdin.
#
# The claude CLI refuses ``--dangerously-skip-permissions`` when
# running as root, so we create a non-root ``agent`` user and ``USER
# agent`` into it. On Windows/macOS Docker Desktop, bind-mounted host
# paths are readable by any container uid (the VFS layer maps perms),
# so the UID of this user doesn't need to match the host user.
#
# PID 1 tails the audit log written by the adapter on the host (via
# the /workspace bind-mount). That turns ``docker logs <container>``
# into a live feed of turn inputs, assistant replies, and tool calls
# — otherwise the image would be a black box because the claude
# subprocess is spawned via docker-exec and its stdout goes to the
# adapter on the host, never to the container's PID 1.
DOCKERFILE = """\
FROM node:22-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \\
        git curl ca-certificates jq ripgrep \\
        python3 python3-pip \\
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code

# Puffo MCP tools server dependencies. `--break-system-packages` is
# required on Debian bookworm — PEP 668 marks /usr as externally
# managed. Installing system-wide is acceptable here: the container
# is single-purpose and disposable.
RUN pip3 install --break-system-packages --no-cache-dir \\
        "mcp>=1.0" "aiohttp>=3.9"

RUN useradd -m -u 2000 -s /bin/bash agent
USER agent
WORKDIR /workspace

# GNU tail -F relies on inotify, and inotify events don't propagate
# through Docker Desktop's host bind-mount on Windows / macOS — so
# `tail -F` on an audit log written from the host sits silent while
# the file grows. This CMD polls the file size every second and
# emits any newly-appended bytes to stdout; docker-logs then streams
# them. Start from current EOF so we don't re-dump the full history
# on every container restart.
CMD ["sh", "-c", "set -eu; mkdir -p /workspace/.puffoagent; touch /workspace/.puffoagent/audit.log; echo \\"[$(date -u +%FT%TZ)] puffo agent=${PUFFO_AGENT_ID:-unknown} container starting; polling /workspace/.puffoagent/audit.log every 1s\\"; last=$(stat -c%s /workspace/.puffoagent/audit.log 2>/dev/null || echo 0); while :; do size=$(stat -c%s /workspace/.puffoagent/audit.log 2>/dev/null || echo 0); if [ \\"$size\\" -gt \\"$last\\" ]; then tail -c +$((last + 1)) /workspace/.puffoagent/audit.log; last=$size; elif [ \\"$size\\" -lt \\"$last\\" ]; then last=0; fi; sleep 1; done"]
"""


class DockerCLIAdapter(Adapter):
    def __init__(
        self,
        agent_id: str,
        model: str,
        image: str,
        workspace_dir: str,
        claude_dir: str,
        session_file: str,
        agent_home_dir: str,
        shared_fs_dir: str,
        mcp_script_dir: str,
        mattermost_url: str = "",
        mattermost_token: str = "",
        team: str = "",
        owner_username: str = "",
    ):
        self.agent_id = agent_id
        self.model = model
        self.image = image or DEFAULT_IMAGE
        self.workspace_dir = workspace_dir
        self.claude_dir = claude_dir
        self.session_file = Path(session_file)
        self.container_name = f"puffo-{agent_id}"
        # This agent's private ``.claude`` dir on the host. The
        # agent_home_dir arg is the agent's virtual $HOME; the
        # ``.claude`` inside it is what we bind-mount into
        # /home/agent/.claude in the container (not the whole home,
        # so the container's default home skeleton — .bashrc etc —
        # stays intact).
        self.agent_home_dir = Path(agent_home_dir)
        self.claude_home_src = self.agent_home_dir / ".claude"
        # Cross-agent cooperation dir, bind-mounted at
        # /workspace/.shared inside the container. All agents on this
        # host see the same mount — an intentional escape hatch from
        # per-agent isolation for file-level coordination.
        self.shared_fs_dir = Path(shared_fs_dir)
        # Host dir holding puffo_tools.py. Bind-mounted read-only into
        # the container at /opt/puffoagent-mcp/ so the claude CLI can
        # spawn the MCP server via `python3 /opt/.../puffo_tools.py`.
        self.mcp_script_dir = Path(mcp_script_dir)
        self.mattermost_url = mattermost_url
        self.mattermost_token = mattermost_token
        self.team = team
        self.owner_username = owner_username
        self._started_lock = asyncio.Lock()
        self._started = False
        self._session: ClaudeSession | None = None

    async def run_turn(self, ctx: TurnContext) -> TurnResult:
        await self._ensure_started()
        session = self._ensure_session()
        user_message = ctx.messages[-1]["content"] if ctx.messages else ""
        return await session.run_turn(user_message, ctx.system_prompt)

    async def warm(self, system_prompt: str) -> None:
        """Start the container + claude subprocess eagerly at daemon
        start. Only spawns the subprocess if this agent has a
        persisted session — fresh agents wait for their first
        message to avoid paying for idle bots. The container itself
        IS started regardless, because ``docker logs`` tailing the
        audit file is useful even for idle agents.
        """
        await self._ensure_started()
        session = self._ensure_session()
        if not session.has_persisted_session():
            logger.info(
                "agent %s: no persisted session; deferring claude spawn until first message",
                self.agent_id,
            )
            return
        await session.warm(system_prompt)

    async def aclose(self) -> None:
        if self._session is not None:
            await self._session.aclose()
            self._session = None
        if not self._started:
            return
        await _run_cmd(["docker", "rm", "-f", self.container_name], check=False)
        self._started = False

    def _ensure_session(self) -> ClaudeSession:
        if self._session is not None:
            return self._session
        extra = self._prepare_mcp_args()
        self._session = ClaudeSession(
            agent_id=self.agent_id,
            session_file=self.session_file,
            build_command=self._build_command,
            # cwd is set via WORKDIR /workspace inside the container;
            # the docker exec subprocess on the host has no
            # meaningful cwd for the claude process.
            cwd=None,
            # Write from the host filesystem — the workspace bind-
            # mount means the in-container tail -F PID 1 picks the
            # same file up and routes it to `docker logs`.
            audit=AuditLog(
                Path(self.workspace_dir) / ".puffoagent" / "audit.log",
                self.agent_id,
            ),
            extra_args=extra,
        )
        return self._session

    def _build_command(self, extra_args: list[str]) -> list[str]:
        cmd = [
            "docker", "exec", "-i", self.container_name,
            "claude", "--dangerously-skip-permissions",
        ]
        if self.model:
            cmd.extend(["--model", self.model])
        cmd.extend(extra_args)
        return cmd

    def _prepare_mcp_args(self) -> list[str]:
        """Write the per-agent MCP config into the workspace so the
        in-container claude picks it up, and return the extra claude
        flags. The container already sandboxes tool calls, so no
        --permission-prompt-tool here; the MCP server is just there
        to expose proactive puffo actions (send_message etc.).
        """
        if not (self.mattermost_url and self.mattermost_token):
            logger.warning(
                "agent %s: cli-docker MCP tools unavailable — no mattermost "
                "URL or bot token; send_message / upload_file disabled",
                self.agent_id,
            )
            return []
        env = mcp_env(
            agent_id=self.agent_id,
            url=self.mattermost_url,
            token=self.mattermost_token,
            workspace="/workspace",  # inside the container
            team=self.team,
            owner_username=self.owner_username,
        )
        # Write to workspace/.puffoagent/mcp-config.json on the host;
        # the container sees the same file at
        # /workspace/.puffoagent/mcp-config.json via the workspace
        # bind-mount.
        config_host = Path(self.workspace_dir) / ".puffoagent" / "mcp-config.json"
        write_cli_mcp_config(
            config_host,
            command="python3",
            args=["/opt/puffoagent-mcp/puffo_tools.py"],
            env=env,
        )
        return ["--mcp-config", "/workspace/.puffoagent/mcp-config.json"]

    async def _ensure_started(self) -> None:
        async with self._started_lock:
            if self._started:
                return
            if shutil.which("docker") is None:
                raise RuntimeError(
                    "docker binary not found on PATH. install Docker Desktop "
                    "(Windows/macOS) or docker-ce (Linux) to use runtime "
                    "kind 'cli-docker'."
                )
            # Seed this agent's per-agent virtual $HOME from the
            # operator's real $HOME on first use. Covers
            # .claude/.credentials.json, .claude/settings.json, and
            # sibling .claude.json. Each agent gets its own copy so
            # sessions/history/cache writes stay isolated per agent.
            host_home = Path.home()
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
                    "run `claude login` on the host, then restart the agent "
                    "— first turn will otherwise fail with an auth error.",
                    self.agent_id, agent_claude, host_home / ".claude",
                )
            await self._ensure_image()
            # Nuke any lingering container from a prior daemon run so
            # the docker-run step below doesn't trip on "name already
            # in use".
            await _run_cmd(["docker", "rm", "-f", self.container_name], check=False)
            await self._start_container()
            self._started = True

    async def _ensure_image(self) -> None:
        rc, _, _ = await _run_cmd(
            ["docker", "image", "inspect", self.image], check=False,
        )
        if rc == 0:
            return
        if self.image != DEFAULT_IMAGE:
            raise RuntimeError(
                f"docker image {self.image!r} not found locally. "
                f"pull it (`docker pull {self.image}`) or clear "
                "runtime.docker_image to use the bundled default."
            )
        logger.info(
            "agent %s: building docker image %s (first use — this may take a few minutes)",
            self.agent_id, self.image,
        )
        await self._build_image()

    async def _build_image(self) -> None:
        proc = await asyncio.create_subprocess_exec(
            "docker", "build", "-t", self.image, "-",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await proc.communicate(DOCKERFILE.encode())
        if proc.returncode != 0:
            tail = stdout.decode("utf-8", errors="replace")[-1500:]
            raise RuntimeError(f"docker build failed:\n{tail}")
        logger.info("agent %s: docker image %s built", self.agent_id, self.image)

    async def _start_container(self) -> None:
        Path(self.workspace_dir).mkdir(parents=True, exist_ok=True)
        # Ensure every bind-mount source exists as a real dir so
        # Docker doesn't auto-create them (which would be owned by
        # root and break the container's ``agent`` user writes).
        self.agent_home_dir.mkdir(parents=True, exist_ok=True)
        (self.agent_home_dir / ".claude").mkdir(parents=True, exist_ok=True)
        # ``.claude.json`` is a FILE sibling to the .claude/ dir.
        # Touch it so the bind-mount below resolves to a real file
        # (without this, Docker creates a directory at the mount
        # target and claude CLI then errors parsing it as JSON).
        agent_claude_json = self.agent_home_dir / ".claude.json"
        agent_claude_json.touch(exist_ok=True)
        self.shared_fs_dir.mkdir(parents=True, exist_ok=True)
        # Write the MCP server script to the mcp_script_dir so it gets
        # bind-mounted into the container. Idempotent — overwrites on
        # every worker start so puffo_tools.py updates take effect
        # without an image rebuild.
        export_mcp_script(self.mcp_script_dir)

        # Two bind-mounts for every cli-docker agent:
        #   1. workspace — per-agent, carries the project-level
        #      .claude/CLAUDE.md that Claude Code auto-discovers.
        #   2. creds — puffoagent-owned OAuth state, shared across
        #      cli-docker agents but isolated from the user's host
        #      ~/.claude.
        # Project-level .claude/ lives INSIDE workspace_dir already,
        # so a single workspace mount covers both project config and
        # session artifacts the agent writes.
        cmd = [
            "docker", "run", "-d",
            "--name", self.container_name,
            "-e", f"PUFFO_AGENT_ID={self.agent_id}",
            # Per-agent project root — agent's workspace lives here,
            # attachments are downloaded here, CLAUDE.md is here.
            "-v", f"{self.workspace_dir}:/workspace",
            # Per-agent claude identity. The agent's private
            # ``.claude`` dir on the host becomes the container's
            # ``/home/agent/.claude`` — isolated credentials,
            # sessions, history, and cache per agent.
            "-v", f"{self.claude_home_src}:/home/agent/.claude",
            # Claude CLI also reads a sibling ``~/.claude.json`` for
            # user-level config; without this mount, the file lives
            # on the container's ephemeral filesystem and is lost on
            # restart, producing a "config file not found" warning
            # every spawn.
            "-v", f"{agent_claude_json}:/home/agent/.claude.json",
            # Cross-agent cooperation dir — every agent sees the
            # same mount at /workspace/.shared. Use for file-level
            # coordination between bots.
            "-v", f"{self.shared_fs_dir}:/workspace/.shared",
            # puffo_tools.py lives on the host (exported from the
            # installed puffoagent package on every worker start),
            # bind-mounted read-only into the container so the
            # claude CLI can spawn it as an MCP server.
            "-v", f"{self.mcp_script_dir}:/opt/puffoagent-mcp:ro",
            "--init",  # reap zombies from claude's child processes
            self.image,
            # NOTE: do NOT pass a command override here. The image's
            # CMD is a polling tail on the audit log so docker logs
            # streams turn events. Passing `sleep infinity` as
            # positional argv clobbers the CMD.
        ]
        rc, _, stderr = await _run_cmd(cmd, check=False)
        if rc != 0:
            raise RuntimeError(
                f"docker run failed for {self.container_name}: "
                f"{stderr.decode('utf-8', errors='replace').strip()[:500]}"
            )


async def _run_cmd(cmd: list[str], check: bool = True) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"command failed ({proc.returncode}): {' '.join(cmd)}\n"
            f"stderr: {stderr.decode('utf-8', errors='replace').strip()[:500]}"
        )
    return proc.returncode, stdout, stderr
