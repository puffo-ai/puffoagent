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
dir, seeded once from the operator's real ``~/.claude`` (settings
only). Bot sessions, history, and the cache stay
inside that dir — no bleed between agents, no bleed back to the
operator's personal claude state. No ``ANTHROPIC_API_KEY`` is
injected.

**OAuth credentials.** The CLI's ``~/.claude/.credentials.json``
specifically is a *single-file* bind-mount of the host's
``~/.claude/.credentials.json`` rather than a per-agent copy.
Anthropic's OAuth uses rotating refresh tokens — each refresh
invalidates the prior refresh token — so per-agent copies would
race each other and constantly re-expire. A single shared file
means whichever process refreshes (host CLI, any agent) updates
the canonical source and every other agent sees the new access
token on its next read. Only ``.credentials.json`` is shared;
sessions, history, settings, and the cache remain per-agent.

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
import json
import logging
import shutil
import time
from pathlib import Path

from ...mcp.config import (
    export_mcp_script,
    mcp_env,
    write_cli_mcp_config,
)
from ...portal.state import (
    seed_claude_home,
    sync_host_mcp_servers,
    sync_host_skills,
)
from .base import Adapter, TurnContext, TurnResult, looks_like_auth_failure
from .cli_session import AuditLog, ClaudeSession


logger = logging.getLogger(__name__)


# Bump this when the Dockerfile changes so existing hosts pick up
# a rebuild without the user having to remember to prune the old
# image tag. ``_ensure_image`` only builds when the tag is missing
# locally, so a stable tag would mask Dockerfile edits.
DEFAULT_IMAGE = "puffo/agent-runtime:v7"

# Timeout for the refresh one-shot. A cold claude + OAuth refresh
# round-trip + one-turn API call normally lands in 5-15s, but can
# stretch past 30s on a busy host or after a cold-cache container
# exec. 120s gives the full chain room without letting a wedged
# subprocess stall the tick forever.
REFRESH_ONESHOT_TIMEOUT_SECONDS = 120

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
#
# ``uv`` ships the ``uvx`` launcher — counterpart of ``npx`` for
# Python-packaged MCP servers. Having both on PATH lets agents
# register any stdio MCP via install_mcp_server without needing
# a per-server pip/npm install step.
#
# ``hermes-agent`` is the second harness we ship (alongside claude-
# code). When an agent sets ``runtime.harness=hermes`` puffoagent
# spawns the interactive ``hermes`` inside this container; it
# auto-discovers our linked ``~/.claude/.credentials.json`` and
# calls the Anthropic API directly. Heads-up: billing routes to
# Anthropic's extra_usage pool (NousResearch/hermes-agent#12905),
# NOT the Claude subscription.
#
# hermes-agent is not published to PyPI — the official install
# paths are a shell installer or a git checkout. We install
# directly from the upstream git repo; ``git`` is already in the
# base apt layer above. Pinned to the v0.10 series; revisit the
# pin when hermes ships a new major.
RUN pip3 install --break-system-packages --no-cache-dir \\
        "mcp>=1.0" "aiohttp>=3.9" "uv>=0.5" \\
     && pip3 install --break-system-packages --no-cache-dir \\
        "git+https://github.com/NousResearch/hermes-agent.git@main"

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
        harness=None,
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
        # Which agent engine runs inside the container. Default is
        # Claude Code — the stream-json + --resume path. Hermes
        # swaps in a one-shot ``hermes chat -q`` per turn. See
        # puffoagent.agent.harness.
        if harness is None:
            from ..harness import ClaudeCodeHarness
            harness = ClaudeCodeHarness()
        self.harness = harness
        self._started_lock = asyncio.Lock()
        self._started = False
        self._session: ClaudeSession | None = None
        # Long-lived ``hermes`` subprocess when harness=hermes.
        # One process per adapter instance; replies drift through
        # stdout while we pipe each turn's user message to stdin.
        self._hermes_proc = None

    async def run_turn(self, ctx: TurnContext) -> TurnResult:
        await self._ensure_started()
        user_message = ctx.messages[-1]["content"] if ctx.messages else ""
        if self.harness.name() == "hermes":
            return await self._run_turn_hermes(user_message, ctx.system_prompt)
        session = self._ensure_session()
        return await session.run_turn(user_message, ctx.system_prompt)

    async def _run_turn_hermes(self, user_message: str, system_prompt: str) -> TurnResult:
        """Hermes turn via a long-lived interactive ``hermes`` subprocess.

        Mirrors the Claude Code pattern: spawn once, keep it alive,
        pipe each turn's user message on stdin, read the reply from
        stdout. Hermes manages its own session continuity (per-HOME
        ``~/.hermes/sessions/``) — we just talk to the running
        process.

        On first ever worker start we spawn bare ``hermes``; on
        daemon restart we spawn ``hermes -c`` to resume the most
        recent session (which, thanks to per-agent HOME isolation,
        is unambiguously this agent's own last session).
        """
        proc = await self._ensure_hermes_proc(system_prompt)
        return await _hermes_turn(proc, user_message, self.agent_id)

    async def _ensure_hermes_proc(self, system_prompt: str):
        """Spawn (or reuse) the long-lived interactive ``hermes`` in
        the container. The container already has HOME pointing at the
        per-agent ``.claude``/``.hermes`` dirs via bind mounts, so
        hermes picks up Claude Code's credentials automatically.

        The ``system_prompt`` is dropped into ``.hermes/SOUL.md`` (the
        hermes equivalent of CLAUDE.md) before first spawn so the
        agent identity carries through every turn without inlining
        it into each user message.
        """
        if self._hermes_proc is not None and self._hermes_proc.returncode is None:
            return self._hermes_proc
        _seed_hermes_soul(self.agent_home_dir, system_prompt)
        _seed_hermes_config(self.agent_home_dir, self.model)
        has_prior_session = self.session_file.exists()
        cmd = ["docker", "exec", "-i", self.container_name, "hermes"]
        if has_prior_session:
            cmd.append("-c")  # --continue, resume most recent session
        logger.info(
            "agent %s: spawning hermes %s",
            self.agent_id, "(resume)" if has_prior_session else "(new)",
        )
        self._hermes_proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        if not has_prior_session:
            try:
                self.session_file.parent.mkdir(parents=True, exist_ok=True)
                self.session_file.write_text(
                    json.dumps({
                        "harness": "hermes",
                        "spawned_at": int(time.time()),
                    }) + "\n",
                    encoding="utf-8",
                )
            except OSError as exc:
                logger.warning(
                    "agent %s: couldn't mark hermes session_file: %s "
                    "(next daemon restart will spawn a fresh session)",
                    self.agent_id, exc,
                )
        return self._hermes_proc

    async def warm(self, system_prompt: str) -> None:
        """Start the container + claude subprocess eagerly at daemon
        start. Only spawns the subprocess if this agent has a
        persisted session — fresh agents wait for their first
        message to avoid paying for idle bots. The container itself
        IS started regardless, because ``docker logs`` tailing the
        audit file is useful even for idle agents.
        """
        await self._ensure_started()
        if self.harness.name() == "hermes":
            # Hermes is one-shot per turn — no persistent subprocess
            # to warm. Container-start above is enough.
            return
        session = self._ensure_session()
        if not session.has_persisted_session():
            logger.info(
                "agent %s: no persisted session; deferring claude spawn until first message",
                self.agent_id,
            )
            return
        await session.warm(system_prompt)

    async def reload(self, new_system_prompt: str) -> None:
        """Close the in-container claude subprocess so the next
        ``run_turn`` spawns a fresh one that re-reads CLAUDE.md.
        The container stays up — we don't pay container cold-start
        on every reload.

        No-op for hermes: each turn is already a fresh process, so
        there's nothing cached to drop.
        """
        if self._session is not None:
            await self._session.aclose()
            self._session = None

    def _credentials_expires_in_seconds(self) -> int | None:
        # The shared-host ``.credentials.json`` is what every
        # cli-docker agent's container reads via bind-mount, so we
        # parse the HOST copy — a refresh inside ANY container
        # writes through to this file and every other container
        # sees the new expiresAt on the next read.
        host_credentials = Path.home() / ".claude" / ".credentials.json"
        try:
            data = json.loads(host_credentials.read_text(encoding="utf-8"))
            expires_ms = int(data["claudeAiOauth"]["expiresAt"])
        except (OSError, ValueError, KeyError, TypeError):
            return None
        return int(expires_ms / 1000 - time.time())

    async def _run_refresh_oneshot(self) -> None:
        """Spawn a short-lived ``docker exec <container> claude
        --print ...`` alongside the long-lived stream-json session.

        The long-lived session refreshes OAuth tokens in memory but
        doesn't rewrite ``.credentials.json`` until the process
        exits — which never happens on the happy path. The one-shot
        process DOES exit, which forces claude-code's credentials-
        write path. The host-bind-mounted file then reflects the
        new token for every sibling agent's next API call.

        ``--max-turns 1`` bounds claude so it can't loop; stream-
        json output guarantees per-event flushing and a clean exit
        on the ``result`` event instead of buffered text-mode
        output that can wedge docker-exec pipes.
        """
        await self._ensure_started()
        cmd = [
            "docker", "exec", self.container_name,
            "claude", "--dangerously-skip-permissions",
            "--print", "--max-turns", "1",
            "--output-format", "stream-json", "--verbose",
        ]
        if self.model:
            cmd.extend(["--model", self.model])
        # Minimal prompt — any text forces the API hit that drives
        # the refresh. The model's reply is discarded.
        cmd.append("ok")
        started_at = time.time()
        try:
            rc, stdout, stderr = await asyncio.wait_for(
                _run_cmd(cmd, check=False),
                timeout=REFRESH_ONESHOT_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "agent %s: refresh one-shot timed out after %ds",
                self.agent_id, REFRESH_ONESHOT_TIMEOUT_SECONDS,
            )
            return
        elapsed = time.time() - started_at
        out_text = stdout.decode("utf-8", errors="replace")
        err_text = stderr.decode("utf-8", errors="replace")
        # Doubles as an inference smoke test — see base adapter's
        # looks_like_auth_failure and the 2026-04-21 incident report.
        if looks_like_auth_failure(out_text, err_text):
            logger.error(
                "agent %s: refresh one-shot hit an auth failure "
                "(rc=%d in %.1fs). operator re-auth likely required. "
                "stdout: %s | stderr: %s",
                self.agent_id, rc, elapsed,
                out_text.strip()[-400:], err_text.strip()[-400:],
            )
            self.auth_healthy = False
        elif rc != 0:
            logger.warning(
                "agent %s: refresh one-shot rc=%d in %.1fs | "
                "stdout: %s | stderr: %s",
                self.agent_id, rc, elapsed,
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
        await _kill_hermes_proc(self._hermes_proc, self.agent_id)
        self._hermes_proc = None
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
            runtime_kind="cli-docker",
            harness=self.harness.name(),
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
            # Note: .credentials.json is *also* seeded but the docker
            # mount below overlays it with the host file so refreshes
            # propagate across agents — see docstring for rationale.
            host_home = Path.home()
            seeded = seed_claude_home(host_home, self.agent_home_dir)
            if seeded:
                logger.info(
                    "agent %s: seeded per-agent virtual $HOME at %s from %s",
                    self.agent_id, self.agent_home_dir, host_home,
                )
            # One-way sync of host-installed user-level skills and MCP
            # registrations into the per-agent virtual $HOME. Agent-
            # installed skills/MCPs stay in the per-agent dir; nothing
            # flows back to the host. Runs every container start so
            # host edits propagate without a daemon restart.
            skill_count = sync_host_skills(host_home, self.agent_home_dir)
            if skill_count:
                logger.info(
                    "agent %s: synced %d host skill(s) into %s",
                    self.agent_id, skill_count,
                    self.agent_home_dir / ".claude" / "skills",
                )
            merged_mcp, unreachable = sync_host_mcp_servers(
                host_home, self.agent_home_dir,
            )
            if merged_mcp:
                logger.info(
                    "agent %s: merged %d host MCP server registration(s) "
                    "into per-agent .claude.json", self.agent_id, merged_mcp,
                )
            for name, cmd in unreachable:
                logger.warning(
                    "agent %s: host MCP %r command %r looks host-local and "
                    "won't resolve inside the container. Install the "
                    "binary in the image or bind-mount it explicitly, "
                    "otherwise this MCP will fail on first use.",
                    self.agent_id, name, cmd,
                )
            if not (host_home / ".claude" / ".credentials.json").exists():
                logger.warning(
                    "agent %s: host has no %s — run `claude login` on the "
                    "host, then restart the agent. First turn will fail "
                    "with an auth error otherwise.",
                    self.agent_id, host_home / ".claude" / ".credentials.json",
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
        # Resolve the host's .credentials.json path for the shared
        # bind-mount below. Existence was already checked in
        # _ensure_started; if the file is missing here we'd hit a
        # confusing docker error, so guard with a touch — an empty
        # file is still a valid mount source even if claude will
        # then fail at auth time with a clearer message.
        host_credentials = Path.home() / ".claude" / ".credentials.json"
        if not host_credentials.exists():
            host_credentials.parent.mkdir(parents=True, exist_ok=True)
            host_credentials.touch()
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

        # Five bind-mounts for every cli-docker agent:
        #   1. workspace        — per-agent project root + cwd.
        #   2. .claude dir      — per-agent claude identity (sessions,
        #                         history, settings, cache).
        #   3. .credentials.json — SHARED with host (single file
        #                         overlay; see docstring).
        #   4. .claude.json     — per-agent CLI user-level config.
        #   5. shared_fs        — cross-agent cooperation dir.
        #   6. mcp_script_dir   — host puffo_tools.py for the MCP
        #                         server (read-only).
        # Project-level .claude/ lives INSIDE workspace_dir already,
        # so the workspace mount covers both project config and
        # session artifacts the agent writes there.
        cmd = [
            "docker", "run", "-d",
            "--name", self.container_name,
            "-e", f"PUFFO_AGENT_ID={self.agent_id}",
            # Per-agent project root — agent's workspace lives here,
            # attachments are downloaded here, CLAUDE.md is here.
            "-v", f"{self.workspace_dir}:/workspace",
            # Per-agent claude identity. The agent's private
            # ``.claude`` dir on the host becomes the container's
            # ``/home/agent/.claude`` — isolated sessions, history,
            # settings, and cache per agent.
            "-v", f"{self.claude_home_src}:/home/agent/.claude",
            # OAuth credentials are SHARED — single-file bind-mount of
            # the host's .credentials.json overlays the per-agent copy
            # inside the .claude dir mount above. Order matters:
            # this mount must come AFTER the dir mount for Docker to
            # treat it as an overlay rather than a no-op. Whichever
            # process refreshes (host or any agent) updates the file
            # in place; every other agent picks up the new access
            # token on its next read. Avoids the rotating-refresh-
            # token race that per-agent copies otherwise hit.
            "-v", f"{host_credentials}:/home/agent/.claude/.credentials.json",
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


# How long to wait after the last stdout byte before deciding
# hermes is done emitting a reply. Conservative — hermes can stream
# tokens with visible latency, and a too-short idle window would
# cut replies off mid-word. 4 s strikes a balance between
# responsiveness and over-aggressive truncation. Revisit if we
# see end-of-turn races in practice.
_HERMES_IDLE_TIMEOUT_SECONDS = 4.0

# Absolute ceiling on how long we'll wait for a hermes reply before
# giving up. Even a multi-tool-call turn shouldn't exceed this.
_HERMES_TURN_TIMEOUT_SECONDS = 300.0


def _seed_hermes_soul(agent_home: Path, system_prompt: str) -> None:
    """Mirror our managed CLAUDE.md-equivalent into ``.hermes/SOUL.md``
    so hermes' interactive session starts with the agent's identity.
    Overwrite every spawn — the system prompt is deterministic and
    regenerating at worker start is how puffoagent stays the
    source of truth for profile + memory layering."""
    if not system_prompt:
        return
    soul = agent_home / ".hermes" / "SOUL.md"
    try:
        soul.parent.mkdir(parents=True, exist_ok=True)
        soul.write_text(system_prompt, encoding="utf-8")
    except OSError as exc:
        logger.warning("couldn't seed hermes SOUL.md at %s: %s", soul, exc)


def _seed_hermes_config(agent_home: Path, model: str) -> None:
    """Write a minimal ``.hermes/config.yaml`` selecting Anthropic as
    the provider so hermes picks up Claude Code's credential store
    (``$HOME/.claude/.credentials.json``, auto-discovered). Lets us
    skip the interactive ``hermes model`` wizard inside the
    container."""
    cfg = agent_home / ".hermes" / "config.yaml"
    try:
        cfg.parent.mkdir(parents=True, exist_ok=True)
        body = ["model:", "  provider: anthropic"]
        if model:
            body.append(f"  default: {model}")
        cfg.write_text("\n".join(body) + "\n", encoding="utf-8")
    except OSError as exc:
        logger.warning("couldn't seed hermes config.yaml at %s: %s", cfg, exc)


async def _kill_hermes_proc(proc, agent_id: str) -> None:
    """Terminate a long-lived hermes subprocess on adapter shutdown.
    No-op if there's no proc or it already exited. Gives hermes 3s
    to flush any in-flight output before SIGKILL.
    """
    if proc is None or proc.returncode is not None:
        return
    try:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
    except (ProcessLookupError, OSError) as exc:
        logger.debug("agent %s: hermes teardown: %s", agent_id, exc)


async def _hermes_turn(proc, user_message: str, agent_id: str):
    """Send ``user_message`` to an open hermes subprocess on stdin,
    then read stdout until it goes idle for _HERMES_IDLE_TIMEOUT
    seconds. Returns a ``TurnResult``.

    Hermes' interactive stdout is free-form text — no structured
    delimiter like claude's stream-json ``result`` event. An idle
    window is the least-bad signal that the agent is done talking.
    If this proves racy in practice we'll upgrade to a prompt-
    marker read (``> `` or similar) once we've seen the actual
    output shape.
    """
    started = time.time()
    if proc.stdin is None or proc.stdout is None:
        return TurnResult(reply="", metadata={
            "error": "hermes subprocess has no stdin/stdout",
        })
    try:
        proc.stdin.write(user_message.encode("utf-8") + b"\n")
        await proc.stdin.drain()
    except (BrokenPipeError, ConnectionResetError) as exc:
        logger.error("agent %s: hermes stdin write failed: %s", agent_id, exc)
        return TurnResult(reply="", metadata={
            "error": f"hermes stdin closed: {exc}",
        })

    deadline = started + _HERMES_TURN_TIMEOUT_SECONDS
    chunks: list[bytes] = []
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            logger.warning(
                "agent %s: hermes turn hit %.0fs ceiling; returning partial reply",
                agent_id, _HERMES_TURN_TIMEOUT_SECONDS,
            )
            break
        try:
            chunk = await asyncio.wait_for(
                proc.stdout.read(4096),
                timeout=min(_HERMES_IDLE_TIMEOUT_SECONDS, remaining),
            )
        except asyncio.TimeoutError:
            # Idle window reached — assume hermes is done.
            break
        if not chunk:
            break  # EOF — process exited mid-turn
        chunks.append(chunk)

    elapsed = time.time() - started
    reply = b"".join(chunks).decode("utf-8", errors="replace").strip()
    logger.info(
        "agent %s: hermes turn done in %.1fs, %d reply chars",
        agent_id, elapsed, len(reply),
    )
    return TurnResult(reply=reply, metadata={"harness": "hermes"})


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
