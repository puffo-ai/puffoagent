"""Docker CLI adapter.

Runs the Claude Code CLI inside a per-agent Docker container. The
container is the sandbox — Claude Code runs with
``--dangerously-skip-permissions`` inside, because escape from the
container back to the host is non-trivial and the user opted into
this model by picking ``kind=cli-docker``.

**Auth.** The claude CLI inside the container uses the same OAuth
credentials the user already set up on the host with ``claude login``.
We bind-mount ``$HOME/.claude`` on the host to ``/root/.claude``
inside the container (read-write). No ``ANTHROPIC_API_KEY`` is
injected.

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
from .cli_session import ClaudeSession

logger = logging.getLogger(__name__)


DEFAULT_IMAGE = "puffo/agent-runtime:latest"

# Kept minimal: node (for the claude CLI npm package), git, the tools
# claude's built-in commands shell out to. No COPY/ADD so the build
# context is empty and we can build from stdin.
#
# We deliberately run as root inside the container. The container
# itself is the sandbox; a non-root user here would just complicate
# the bind-mount of ~/.claude from the host (OAuth creds owned by the
# host user's UID don't line up with an in-container 'agent' user).
DOCKERFILE = """\
FROM node:22-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \\
        git curl ca-certificates jq ripgrep \\
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code

WORKDIR /workspace

CMD ["sleep", "infinity"]
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
    ):
        self.agent_id = agent_id
        self.model = model
        self.image = image or DEFAULT_IMAGE
        self.workspace_dir = workspace_dir
        self.claude_dir = claude_dir
        self.session_file = Path(session_file)
        self.container_name = f"puffo-{agent_id}"
        self.host_claude_creds_dir = str(Path.home() / ".claude")
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
            creds = Path(self.host_claude_creds_dir) / ".credentials.json"
            if not creds.exists():
                logger.warning(
                    "agent %s: %s not found. run `claude login` on the host "
                    "once before this agent's first turn or the container's "
                    "claude CLI will fail with an auth error.",
                    self.agent_id, creds,
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
        # Create host ~/.claude if missing so the bind-mount target
        # resolves to a real dir (Docker would otherwise auto-create
        # it owned by root, which trips claude login later on).
        Path(self.host_claude_creds_dir).mkdir(parents=True, exist_ok=True)

        # Project-level .claude/ lives INSIDE workspace_dir (that's
        # what Claude Code's project discovery looks for), so the
        # workspace mount below carries it for free — no second mount
        # needed. User-level claude state (OAuth credentials, global
        # settings) is separate and comes from the host's ~/.claude.
        cmd = [
            "docker", "run", "-d",
            "--name", self.container_name,
            "-v", f"{self.workspace_dir}:/workspace",
            "-v", f"{self.host_claude_creds_dir}:/root/.claude",
            "--init",  # reap zombies from claude's child processes
            self.image,
            "sleep", "infinity",
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
