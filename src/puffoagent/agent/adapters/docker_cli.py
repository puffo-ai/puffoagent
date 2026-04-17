"""Docker CLI adapter.

Runs the Claude Code CLI inside a per-agent Docker container. The
container is the sandbox — Claude Code runs with
``--dangerously-skip-permissions`` inside, because escape from the
container back to the host is non-trivial and the user opted into
this model by picking ``kind=cli-docker``.

**Auth.** The claude CLI inside the container reads OAuth credentials
from ``/home/agent/.claude``, which is bind-mounted from
``~/.puffoagent/docker/creds`` on the host. On first cli-docker use
we seed that dir by copying ``.credentials.json`` (and
``.claude.json`` if present) from the user's personal ``~/.claude``
— so a one-time ``claude login`` on the host covers every cli-docker
agent. Keeping a puffoagent-owned copy separates bot activity
(session caches, history, refreshed tokens) from the user's personal
Claude Code state. No ``ANTHROPIC_API_KEY`` is injected.

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
    default_python_executable,
    export_mcp_script,
    mcp_env,
    write_cli_mcp_config,
)
from .base import Adapter, TurnContext, TurnResult
from .cli_session import AuditLog, ClaudeSession


# Filenames to lift from the host's ~/.claude on first seed. Limited
# to the OAuth-relevant set to avoid copying multi-MB caches /
# history the user doesn't want shared with their bots.
SEED_FILES = (".credentials.json", ".claude.json", "settings.json")


def seed_docker_creds(host_claude_dir: Path, puffo_creds_dir: Path) -> bool:
    """Copy OAuth-essential files from the host's personal Claude Code
    state into puffoagent's own creds dir, so cli-docker containers
    can authenticate without bind-mounting the user's full ~/.claude.

    Idempotent: existing files in ``puffo_creds_dir`` are left alone
    (so refreshed tokens from previous bot runs survive). Returns
    True if we actually copied anything — used only for diagnostic
    logging.
    """
    puffo_creds_dir.mkdir(parents=True, exist_ok=True)
    copied_any = False
    for name in SEED_FILES:
        src = host_claude_dir / name
        dst = puffo_creds_dir / name
        if dst.exists() or not src.exists():
            continue
        try:
            shutil.copy2(src, dst)
            copied_any = True
        except OSError as exc:
            logger.warning("seed %s -> %s failed: %s", src, dst, exc)
    return copied_any

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
        creds_dir: str,
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
        # puffoagent-owned OAuth state, bind-mounted into the container.
        # Seeded from the host's personal ~/.claude on first use — see
        # ``seed_docker_creds``. Writes from bot sessions stay here
        # and don't bleed into the user's host state.
        self.creds_dir = Path(creds_dir)
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
        if self._session is None:
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
        user_message = ctx.messages[-1]["content"] if ctx.messages else ""
        return await self._session.run_turn(user_message, ctx.system_prompt)

    async def aclose(self) -> None:
        if self._session is not None:
            await self._session.aclose()
            self._session = None
        if not self._started:
            return
        await _run_cmd(["docker", "rm", "-f", self.container_name], check=False)
        self._started = False

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
            # Seed puffoagent-owned creds from the user's host
            # ~/.claude on first cli-docker use. If they haven't
            # ``claude login``'d and we find no creds in either place,
            # warn up-front so the auth failure at first turn has
            # context.
            host_claude = Path.home() / ".claude"
            seeded = seed_docker_creds(host_claude, self.creds_dir)
            if seeded:
                logger.info(
                    "agent %s: seeded %s from host's ~/.claude (first cli-docker use)",
                    self.agent_id, self.creds_dir,
                )
            if not (self.creds_dir / ".credentials.json").exists():
                logger.warning(
                    "agent %s: no .credentials.json in %s (and none at %s). "
                    "run `claude login` on the host, then restart the "
                    "agent — first turn will otherwise fail with an auth error.",
                    self.agent_id, self.creds_dir, host_claude,
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
        # Ensure creds dir exists so the bind-mount target resolves to
        # a real dir (Docker would otherwise auto-create it owned by
        # root, which trips ``claude login`` later).
        self.creds_dir.mkdir(parents=True, exist_ok=True)
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
            "-v", f"{self.workspace_dir}:/workspace",
            "-v", f"{self.creds_dir}:/home/agent/.claude",
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
