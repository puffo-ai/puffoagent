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
DEFAULT_IMAGE = "puffo/agent-runtime:v4"

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
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code

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
        self._started_lock = asyncio.Lock()
        self._started = False
        self._session: ClaudeSession | None = None

    async def run_turn(self, ctx: TurnContext) -> TurnResult:
        await self._ensure_started()
        if self._session is None:
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
