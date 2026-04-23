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
import re
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
    sync_host_gemini_mcp_servers,
    sync_host_gemini_skills,
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
DEFAULT_IMAGE = "puffo/agent-runtime:v9"

# Pinned version of the Claude Code CLI baked into the image.
# Floating (``npm install -g @anthropic-ai/claude-code``) was bad
# hygiene: each rebuild could pick up an upstream release that
# shifts the stream-json protocol or the ``--permission-mode``
# semantics under our feet. Bump this pin deliberately when a new
# upstream release is verified against ``tests/``.
CLAUDE_CODE_NPM_VERSION = "2.1.117"

# Pinned version of the Gemini CLI (``@google/gemini-cli``). Same
# reproducibility rationale as CLAUDE_CODE_NPM_VERSION above.
# Verify against ``tests/`` and a live ``hermes``-style smoke test
# before bumping.
GEMINI_CLI_NPM_VERSION = "0.38.2"

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

RUN npm install -g \\
        @anthropic-ai/claude-code@__CLAUDE_CODE_VERSION__ \\
        @google/gemini-cli@__GEMINI_CLI_VERSION__

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
""".replace(
    "__CLAUDE_CODE_VERSION__", CLAUDE_CODE_NPM_VERSION,
).replace(
    "__GEMINI_CLI_VERSION__", GEMINI_CLI_NPM_VERSION,
)


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
        google_api_key: str = "",
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
        # Only consulted when harness is gemini-cli — passed to the
        # containerised ``gemini`` CLI via ``docker exec -e
        # GEMINI_API_KEY=...`` per turn. Claude Code and hermes don't
        # use it.
        self.google_api_key = google_api_key
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
        # Has the puffo MCP server been registered with the
        # container's hermes config yet? Flipped by
        # ``_ensure_hermes_mcp_registered`` on first turn after a
        # worker restart. Registration is idempotent (remove + add)
        # so re-runs are safe if the flag gets out of sync. (The
        # gemini path doesn't need this — its MCP registration is
        # written upfront to ``.gemini/settings.json`` by the host
        # sync in ``_ensure_started``.)
        self._hermes_mcp_registered = False

    async def run_turn(self, ctx: TurnContext) -> TurnResult:
        await self._ensure_started()
        user_message = ctx.messages[-1]["content"] if ctx.messages else ""
        if self.harness.name() == "hermes":
            return await self._run_turn_hermes(user_message, ctx.system_prompt)
        if self.harness.name() == "gemini-cli":
            return await self._run_turn_gemini(user_message, ctx.system_prompt)
        session = self._ensure_session()
        return await session.run_turn(user_message, ctx.system_prompt)

    async def _run_turn_hermes(self, user_message: str, system_prompt: str) -> TurnResult:
        """One-shot hermes turn via ``hermes chat --provider anthropic
        --quiet [--continue] -q <prompt>``.

        Why not a long-lived interactive subprocess like Claude Code:
        hermes' interactive mode requires a real TTY and treats EOF
        on piped stdin as "user quit". There is no stream-json-style
        line protocol. The supported programmatic path is the
        ``chat -q`` single-query form; multi-turn continuity works
        through hermes' own on-disk session store + ``--continue``.
        Cold start per turn is ~3-7 s.

        Authentication: zero-config. Hermes auto-discovers the
        Claude Code credential file we already bind-mount at
        ``~/.claude/.credentials.json`` inside the container. The
        access token rotates with the host's claude login; we don't
        touch hermes' own auth store.

        Session continuity:

          * First turn: no ``--continue``; inline the system prompt
            in the query text since hermes has no ``--system`` flag.
          * Subsequent turns: ``--continue`` tells hermes to resume
            its most recent session for this HOME.
          * Sentinel: ``cli_session.json`` records "have we done at
            least one turn". Stale sentinel (daemon restarted but
            hermes' state.db missing / pruned) yields a
            ``No previous CLI session found to continue`` error;
            we detect that, clear the sentinel, and retry once.
        """
        return await self._run_hermes_chat(user_message, system_prompt)

    async def _ensure_hermes_mcp_registered(self) -> None:
        """Register the puffo MCP server with the in-container hermes
        config so chat turns can call ``send_message`` /
        ``get_channel_history`` / other puffo tools.

        Same tool surface claude-code agents get via ``--mcp-config``;
        hermes uses its own ``hermes mcp add`` registry persisted at
        ``/home/agent/.hermes/config.yaml``. That path lives in the
        bind-mounted agent_home dir, so registration survives
        container restarts within a daemon lifetime — but we
        re-register on every adapter-instance start anyway so a
        rotated bot token or a changed config shape is picked up
        automatically.

        The registration is done via ``hermes mcp add`` which,
        annoyingly, prompts "Enable all N tools? [Y/n/select]"
        before writing config. We pipe ``y\\n`` on stdin to accept.
        Silent failure just disables tool calling for this session
        (hermes will still reply, just without tools); we log but
        don't hard-fail the turn.
        """
        if self._hermes_mcp_registered:
            return
        env = mcp_env(
            agent_id=self.agent_id,
            url=self.mattermost_url,
            token=self.mattermost_token,
            workspace="/workspace",
            team=self.team,
            owner_username=self.owner_username,
            runtime_kind="cli-docker",
            harness="hermes",
        )
        env_flags: list[str] = [f"{k}={v}" for k, v in env.items()]

        # Remove an existing puffo registration first (from a prior
        # worker lifetime) so the add below overwrites stale env
        # (rotated bot token, renamed agent, etc.) cleanly. rc!=0
        # is fine — means there wasn't one.
        await _run_cmd(
            [
                "docker", "exec", self.container_name,
                "hermes", "mcp", "remove", "puffo",
            ],
            check=False,
        )

        cmd = [
            "docker", "exec", "-i", self.container_name,
            "hermes", "mcp", "add", "puffo",
            "--command", "python3",
            "--args", "/opt/puffoagent-mcp/puffo_tools.py",
            "--env", *env_flags,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate(b"y\n")
        except Exception as exc:
            logger.warning(
                "agent %s: couldn't register puffo MCP with hermes: %s "
                "(chat will work, tool calls won't)",
                self.agent_id, exc,
            )
            return
        if proc.returncode != 0:
            logger.warning(
                "agent %s: hermes mcp add puffo rc=%d | stdout: %s | stderr: %s "
                "(chat will work, tool calls won't)",
                self.agent_id, proc.returncode,
                stdout.decode("utf-8", errors="replace").strip()[-400:],
                stderr.decode("utf-8", errors="replace").strip()[-400:],
            )
            return
        logger.info(
            "agent %s: registered puffo MCP server with hermes "
            "(18 tools available via hermes chat)",
            self.agent_id,
        )
        self._hermes_mcp_registered = True

    async def _run_hermes_chat(
        self, user_message: str, system_prompt: str, *, _retried: bool = False,
    ) -> TurnResult:
        # The upstream docs claim hermes auto-discovers Claude Code's
        # credential file at ``~/.claude/.credentials.json``. In
        # practice (verified on a fresh v7 container) it does NOT —
        # ``hermes auth list`` comes up empty on first invocation and
        # ``hermes chat`` errors with "It looks like Hermes isn't
        # configured yet -- no API keys or providers found."
        #
        # Workaround: read the access token out of the credentials
        # file on the host and pass it to hermes via
        # ``ANTHROPIC_API_KEY`` on the ``docker exec`` command. The
        # file is kept fresh by Claude Code's own OAuth refresh
        # machinery (it's bind-mounted from the host's
        # ``~/.claude/.credentials.json``), so every turn reads the
        # current token — no per-agent hermes state to maintain.
        #
        # ``sk-ant-oat01-...`` tokens are API-compatible with
        # Anthropic's regular ``sk-ant-api03-...`` keys; hermes
        # happily sends them with Bearer auth. Billing routes to
        # Anthropic's ``extra_usage`` pool per
        # NousResearch/hermes-agent#12905 — not your Claude
        # subscription.
        token = _read_claude_access_token()
        if not token:
            logger.error(
                "agent %s: cannot read Claude Code access token from "
                "%s — hermes turn would fail with no credentials. "
                "run `claude login` on the host to refresh.",
                self.agent_id, _HOST_CLAUDE_CREDENTIALS_PATH,
            )
            return TurnResult(reply="", metadata={
                "error": "no Claude Code access token available on host",
            })

        # Make sure hermes knows about the puffo MCP server so the
        # agent can call send_message / get_channel_history / etc.
        # during this turn. Idempotent — skipped after the first
        # successful registration per adapter instance.
        await self._ensure_hermes_mcp_registered()

        has_prior_session = self.session_file.exists()
        prompt = user_message if has_prior_session else _stitch_hermes_prompt(
            system_prompt, user_message,
        )
        cmd = [
            "docker", "exec", "-i",
            # Put the token in argv-space to docker. Host-local
            # visibility is acceptable (the daemon user already has
            # read access to the credentials file). If we ever run
            # the daemon on a shared host we should switch to
            # --env-file + a short-lived tmpfile instead.
            "-e", f"ANTHROPIC_API_KEY={token}",
            self.container_name,
            "hermes", "chat",
            "--provider", "anthropic",
            "--quiet",
            "--source", f"puffoagent:{self.agent_id}",
            "--model", _hermes_model_id(self.model),
        ]
        if has_prior_session:
            cmd.append("--continue")
        cmd.extend(["-q", prompt])

        started = time.time()
        rc, stdout, stderr = await _run_cmd(cmd, check=False)
        elapsed = time.time() - started
        stdout_text = stdout.decode("utf-8", errors="replace")
        stderr_text = stderr.decode("utf-8", errors="replace")

        # Stale sentinel: hermes doesn't have a session matching our
        # "we've done a turn before" marker. Clear + retry once with
        # --continue dropped. ``_retried`` guards against loops if
        # the error somehow persists on fresh spawn.
        if (
            rc != 0
            and _HERMES_NO_RESUME_SIGNATURE in stdout_text
            and not _retried
        ):
            logger.info(
                "agent %s: hermes rejected --continue; clearing sentinel and retrying fresh",
                self.agent_id,
            )
            try:
                self.session_file.unlink()
            except OSError:
                pass
            return await self._run_hermes_chat(
                user_message, system_prompt, _retried=True,
            )

        if rc != 0:
            logger.error(
                "agent %s: hermes turn rc=%d in %.1fs | stdout: %r | stderr: %s",
                self.agent_id, rc, elapsed,
                stdout_text.strip()[:400],
                stderr_text.strip()[-400:] or "(empty)",
            )
            return TurnResult(reply="", metadata={
                "error": f"hermes exited rc={rc}",
                "stdout_snippet": stdout_text[:400],
                "stderr_tail": stderr_text[-400:],
            })

        reply, session_id = _parse_hermes_reply(stdout_text)
        if not reply:
            logger.warning(
                "agent %s: hermes rc=0 but parser found no reply. "
                "stdout: %r", self.agent_id, stdout_text[:400],
            )

        # First-ever successful turn: drop the sentinel so subsequent
        # turns pass --continue. Include the session_id for
        # post-hoc debugging (lets us cross-reference hermes'
        # sessions/ dir with our agent).
        if not has_prior_session:
            try:
                self.session_file.parent.mkdir(parents=True, exist_ok=True)
                self.session_file.write_text(
                    json.dumps({
                        "harness": "hermes",
                        "session_id": session_id,
                        "first_turn_at": int(time.time()),
                    }) + "\n",
                    encoding="utf-8",
                )
            except OSError as exc:
                logger.warning(
                    "agent %s: couldn't write hermes session_file: %s "
                    "(next turn will start a fresh session)",
                    self.agent_id, exc,
                )

        logger.info(
            "agent %s: hermes turn rc=0 in %.1fs, %d reply chars, "
            "session=%s, resume=%s",
            self.agent_id, elapsed, len(reply), session_id or "?",
            has_prior_session,
        )
        return TurnResult(reply=reply, metadata={
            "harness": "hermes",
            "session_id": session_id,
        })

    # ── Gemini harness ────────────────────────────────────────────

    async def _run_turn_gemini(
        self, user_message: str, system_prompt: str,
    ) -> TurnResult:
        """One-shot gemini-cli turn via ``gemini -p <prompt>
        --output-format json [-r latest]``.

        Structured like the hermes path: a thin entry that delegates
        to a ``_run_gemini_chat`` helper which handles the stale-
        resume retry.

        Auth: ``GEMINI_API_KEY`` read from daemon.yml's
        ``google.api_key`` and passed via ``docker exec -e``. Unlike
        hermes we do NOT piggyback on Claude Code's credential file
        — Google's API keys and Anthropic's OAuth tokens are
        unrelated identity spaces.

        Session continuity: gemini's ``-r latest`` resumes the most
        recent session for this project (keyed by cwd hash under
        ``~/.gemini/``). We use our ``cli_session.json`` sentinel to
        decide whether to pass ``-r`` at all — same pattern as
        hermes. Stale sentinel → fall back to a fresh session.

        Persona + memory: written to ``<agent_home>/.gemini/GEMINI.md``
        by the worker on every start; gemini auto-discovers it via
        ``$HOME/.gemini/GEMINI.md`` on each turn. No first-turn
        stitching needed.

        MCP tools: registered upfront in
        ``<workspace>/.gemini/settings.json`` (project scope —
        gemini's MCP resolver defaults to cwd, not $HOME) by
        ``sync_host_gemini_mcp_servers``. Same file gets any host
        user-level MCPs merged in during the same write, so agents
        inherit both the operator's gemini MCPs and the puffo one.
        """
        return await self._run_gemini_chat(user_message, system_prompt)

    async def _run_gemini_chat(
        self, user_message: str, system_prompt: str, *, _retried: bool = False,
    ) -> TurnResult:
        if not self.google_api_key:
            logger.error(
                "agent %s: gemini-cli turn requires google.api_key in "
                "daemon.yml (passed as GEMINI_API_KEY to the container). "
                "run `puffoagent init` or edit daemon.yml to set it.",
                self.agent_id,
            )
            return TurnResult(reply="", metadata={
                "error": "no google api_key configured in daemon.yml",
            })

        # Persona + memory arrive via ``~/.gemini/GEMINI.md`` (written
        # by the worker on every start) and the puffo MCP entry lives
        # in ``~/.gemini/settings.json`` (written by
        # ``sync_host_gemini_mcp_servers`` in ``_ensure_started``),
        # so there's no first-turn stitching or runtime subprocess
        # registration to do here — just send the user message.
        has_prior_session = self.session_file.exists()
        cmd = _build_gemini_argv(
            container_name=self.container_name,
            api_key=self.google_api_key,
            model=self.model,
            has_prior_session=has_prior_session,
            user_message=user_message,
        )

        # Log the full argv (with the API key redacted) so a failed
        # turn is reproducible from the daemon log alone. Each argv
        # element becomes one space-separated token in the log so
        # operators can tell "--prompt=..." from a separate "-p"
        # invocation at a glance.
        redacted = [
            "GEMINI_API_KEY=***" if a.startswith("GEMINI_API_KEY=") else a
            for a in cmd
        ]
        logger.info("agent %s: gemini argv: %s", self.agent_id, " ".join(redacted))

        started = time.time()
        rc, stdout, stderr = await _run_cmd(cmd, check=False)
        elapsed = time.time() - started
        stdout_text = stdout.decode("utf-8", errors="replace")
        stderr_text = stderr.decode("utf-8", errors="replace")

        # Stale sentinel recovery: if ``-r latest`` failed and we
        # haven't already retried, clear the sentinel and start a
        # fresh session. We're deliberately permissive here — any
        # error with ``-r`` in play triggers retry — because the
        # upstream docs don't pin an error string to key off and
        # "fresh start" is always safer than a hard turn failure.
        if rc != 0 and has_prior_session and not _retried:
            logger.info(
                "agent %s: gemini -r latest rc=%d; clearing sentinel "
                "and retrying with a fresh session. stderr: %s",
                self.agent_id, rc, stderr_text.strip()[-200:] or "(empty)",
            )
            try:
                self.session_file.unlink()
            except OSError:
                pass
            return await self._run_gemini_chat(
                user_message, system_prompt, _retried=True,
            )

        if rc != 0:
            logger.error(
                "agent %s: gemini turn rc=%d in %.1fs | stdout: %r | stderr: %s",
                self.agent_id, rc, elapsed,
                stdout_text.strip()[:400],
                stderr_text.strip()[-400:] or "(empty)",
            )
            return TurnResult(reply="", metadata={
                "error": f"gemini exited rc={rc}",
                "stdout_snippet": stdout_text[:400],
                "stderr_tail": stderr_text[-400:],
            })

        reply, session_id, err = _parse_gemini_reply(stdout_text)
        if err:
            logger.warning(
                "agent %s: gemini rc=0 but returned JSON error: %s",
                self.agent_id, err,
            )
        if not reply:
            logger.warning(
                "agent %s: gemini rc=0 but parser found no reply. "
                "stdout: %r", self.agent_id, stdout_text[:400],
            )

        if not has_prior_session:
            try:
                self.session_file.parent.mkdir(parents=True, exist_ok=True)
                self.session_file.write_text(
                    json.dumps({
                        "harness": "gemini-cli",
                        "session_id": session_id,
                        "first_turn_at": int(time.time()),
                    }) + "\n",
                    encoding="utf-8",
                )
            except OSError as exc:
                logger.warning(
                    "agent %s: couldn't write gemini session_file: %s "
                    "(next turn will start a fresh session)",
                    self.agent_id, exc,
                )

        logger.info(
            "agent %s: gemini turn rc=0 in %.1fs, %d reply chars, "
            "session=%s, resume=%s%s",
            self.agent_id, elapsed, len(reply), session_id or "?",
            has_prior_session,
            f", err={err!r}" if err else "",
        )
        metadata: dict = {
            "harness": "gemini-cli",
            "session_id": session_id,
        }
        if err:
            metadata["error"] = err
        return TurnResult(reply=reply, metadata=metadata)

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

            # Gemini-side host sync — skills tree + ``mcpServers``
            # merged from ``~/.gemini/settings.json``. Always runs
            # (cheap when the host has no ~/.gemini/) so swapping
            # harness to gemini-cli doesn't need another container
            # rebuild.
            #
            # Target is the PROJECT-SCOPE gemini dir under the
            # workspace (``<workspace>/.gemini/``), not the user-
            # scope ``<agent_home>/.gemini/``. Gemini's MCP resolver
            # defaults to project scope — verified empirically by
            # running ``gemini mcp add`` inside the container without
            # a ``--scope`` flag: it writes to ``<cwd>/.gemini/
            # settings.json``, and ``gemini mcp list`` from that cwd
            # ignores anything in ``$HOME/.gemini/settings.json``.
            # Writing to user scope silently dropped our puffo MCP
            # and no tool calls reached the agent.
            gemini_project_dir = Path(self.workspace_dir)
            gemini_skill_count = sync_host_gemini_skills(
                host_home, gemini_project_dir,
            )
            if gemini_skill_count:
                logger.info(
                    "agent %s: synced %d host gemini skill(s) into %s",
                    self.agent_id, gemini_skill_count,
                    gemini_project_dir / ".gemini" / "skills",
                )
            # Inject the puffo MCP entry in the same write so the
            # project settings.json is consistent in one pass —
            # no separate ``gemini mcp add`` subprocess to race.
            puffo_entry = _puffo_gemini_mcp_entry(
                agent_id=self.agent_id,
                mattermost_url=self.mattermost_url,
                mattermost_token=self.mattermost_token,
                team=self.team,
                owner_username=self.owner_username,
            )
            merged_gemini_mcp, gemini_unreachable = sync_host_gemini_mcp_servers(
                host_home, gemini_project_dir,
                extra_servers={"puffo": puffo_entry} if puffo_entry else None,
            )
            if merged_gemini_mcp:
                logger.info(
                    "agent %s: merged %d host gemini MCP server "
                    "registration(s) into .gemini/settings.json",
                    self.agent_id, merged_gemini_mcp,
                )
            for name, cmd in gemini_unreachable:
                logger.warning(
                    "agent %s: host gemini MCP %r command %r looks "
                    "host-local and won't resolve inside the container. "
                    "Install the binary in the image or bind-mount it, "
                    "otherwise the MCP will fail on first use.",
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
        if await _image_exists_locally(self.image):
            return
        if self.image != DEFAULT_IMAGE:
            raise RuntimeError(
                f"docker image {self.image!r} not found locally. "
                f"pull it (`docker pull {self.image}`) or clear "
                "runtime.docker_image to use the bundled default."
            )
        # Daemon-wide lock so N agents that all tick past
        # ``_ensure_started`` at the same time don't each kick off
        # their own ``docker build -t <image>``. Concurrent builds
        # against the same tag race in BuildKit's exporter — the
        # loser gets "image already exists" and crashes its worker.
        # Policy: the first agent to grab the lock runs the build;
        # the others wait, then re-check (likely see the image now
        # and skip).
        async with _BUILD_LOCK:
            if await _image_exists_locally(self.image):
                logger.info(
                    "agent %s: image %s was built by another worker "
                    "during our wait — skipping rebuild",
                    self.agent_id, self.image,
                )
                return
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
        # ``.gemini/`` is a DIR. Pre-create it so the bind-mount
        # below resolves to an existing path — otherwise Docker
        # creates one owned by root at mount time, which the non-
        # root ``agent`` user can't write to.
        (self.agent_home_dir / ".gemini").mkdir(parents=True, exist_ok=True)
        self.shared_fs_dir.mkdir(parents=True, exist_ok=True)
        # Write the MCP server script to the mcp_script_dir so it gets
        # bind-mounted into the container. Idempotent — overwrites on
        # every worker start so puffo_tools.py updates take effect
        # without an image rebuild.
        export_mcp_script(self.mcp_script_dir)

        # Seven bind-mounts for every cli-docker agent:
        #   1. workspace        — per-agent project root + cwd.
        #   2. .claude dir      — per-agent claude identity (sessions,
        #                         history, settings, cache).
        #   3. .credentials.json — SHARED with host (single file
        #                         overlay; see docstring).
        #   4. .claude.json     — per-agent CLI user-level config.
        #   5. .gemini dir      — per-agent gemini identity (GEMINI.md,
        #                         skills, settings.json with mcpServers).
        #   6. shared_fs        — cross-agent cooperation dir.
        #   7. mcp_script_dir   — host puffo_tools.py for the MCP
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
            # Per-agent gemini identity. Mirrors the .claude mount
            # above — ``GEMINI.md`` (managed by the worker), synced
            # host skills under ``.gemini/skills/``, and host+puffo
            # MCP registrations in ``.gemini/settings.json`` all live
            # under ``<agent_home>/.gemini/`` and land at
            # ``/home/agent/.gemini`` inside the container. Always
            # mounted regardless of harness so swapping to gemini-
            # cli doesn't need a rebuild; empty when unused.
            "-v", f"{self.agent_home_dir / '.gemini'}:/home/agent/.gemini",
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


# Daemon-wide lock around ``docker build -t <tag>``. Serialises
# concurrent cli-docker workers that all hit ``_ensure_image`` at
# the same time (e.g. right after an image-tag bump like v6 -> v7).
# Without it BuildKit's exporter races when two builds produce the
# same tag, and one worker bails out with
# ``image "<repo>:<tag>": already exists``.
_BUILD_LOCK = asyncio.Lock()


async def _image_exists_locally(tag: str) -> bool:
    rc, _, _ = await _run_cmd(
        ["docker", "image", "inspect", tag], check=False,
    )
    return rc == 0


# hermes prints this exact line to stdout and exits rc=1 when
# ``--continue`` is passed but its session store has nothing to
# resume. Our session-exists sentinel (``cli_session.json``) can
# get out of sync — e.g. ``~/.hermes/sessions/`` was pruned — so
# we detect the case and fall back to a fresh ``hermes chat``.
_HERMES_NO_RESUME_SIGNATURE = "No previous CLI session found to continue"

# Banner / metadata lines hermes --quiet emits before the reply
# body. We skip any line matching these to get at the actual
# response text.
#
# Examples observed in the wild:
#
#   ⚠️  Normalized model 'anthropic/claude-opus-4-6' to 'claude-opus-4-6' for
#   anthropic.
#   ↻ Resumed session 20260422_222809_425056 (1 user message, 2 total messages)
#   session_id: 20260422_213753_5d42f9
#
# The session id occurs in either the ``Resumed session`` line
# (when ``--continue`` succeeds) or the standalone ``session_id:``
# line (on older hermes builds / sometimes on the first turn). It
# can also be absent entirely on a fresh session — we tolerate that
# and return the reply with session_id="".
_HERMES_SESSION_ID_RE = re.compile(r"^session_id:\s*(\S+)\s*$")
_HERMES_RESUMED_SESSION_RE = re.compile(
    r"^↻\s*Resumed session\s+(\S+).*$"
)
_HERMES_MODEL_NORMALISED_RE = re.compile(
    r"^⚠️\s+Normalized model .*$"
)
# Continuation line of the "Normalized model" banner — the value
# gets wrapped and the tail lands on its own line. Match exactly
# ``anthropic.`` (or any bare provider name followed by a period)
# so we don't accidentally eat reply text that happens to start
# with a period.
_HERMES_MODEL_NORMALISED_TAIL_RE = re.compile(r"^[a-z0-9\-]+\.$")


# Where Claude Code stores its OAuth credentials on the host. We
# read the access token from here on every hermes turn because
# hermes' own auto-discovery is unreliable on a fresh container
# (see ``_run_hermes_chat``). The file is kept fresh by Claude
# Code's OAuth refresh path, which puffoagent's
# ``refresh_ping`` + ``link_host_credentials`` already coordinate.
_HOST_CLAUDE_CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"


def _read_claude_access_token() -> str:
    """Extract the current Claude Code OAuth access token from the
    host's credentials file so we can hand it to hermes via
    ``ANTHROPIC_API_KEY``.

    Returns the empty string on any failure (missing file, malformed
    JSON, missing key). Caller logs the condition and surfaces a
    turn-level error rather than erroring the worker — the operator
    can re-run ``claude login`` without a daemon restart.
    """
    try:
        data = json.loads(
            _HOST_CLAUDE_CREDENTIALS_PATH.read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return ""
    return ((data.get("claudeAiOauth") or {}).get("accessToken") or "").strip()


def _hermes_model_id(model: str) -> str:
    """Translate the agent's ``runtime.model`` into the ``<provider>/
    <model>`` form ``hermes chat --model`` expects.

    Strips Claude-Code-isms we might carry forward (e.g. the
    ``[1m]`` 1M-context suffix on ``claude-opus-4-6[1m]``) since
    hermes rejects unrecognised suffixes. Prepends ``anthropic/``
    if the caller didn't. Empty / missing → a reasonable default.
    """
    base = (model or "").split("[", 1)[0].strip()
    if not base:
        return "anthropic/claude-opus-4-6"
    return base if "/" in base else f"anthropic/{base}"


def _stitch_hermes_prompt(system_prompt: str, user_message: str) -> str:
    """Hermes ``chat -q`` has no ``--system`` equivalent. On the first
    turn of a session we inline the system prompt above the user
    message with a visible separator so Claude can distinguish
    persona from request. On subsequent turns ``--continue`` carries
    session context and the caller passes just the user_message
    through — no need to re-send the system prompt."""
    if not system_prompt:
        return user_message
    return f"{system_prompt}\n\n---\n\n{user_message}"


def _parse_hermes_reply(stdout_text: str) -> tuple[str, str]:
    """Pull the reply + session id out of ``hermes chat --quiet``
    stdout.

    Observed shapes in the wild:

        # older build / some first-turns:
        ⚠️  Normalized model '...' to '...' for
        anthropic.

        session_id: 20260422_213753_5d42f9
        <reply>

        # --continue case:
        ⚠️  Normalized model '...' to '...' for
        anthropic.
        ↻ Resumed session 20260422_222809_425056 (1 user message, 2 total messages)
        <reply>

        # some fresh sessions (no session_id printed at all):
        ⚠️  Normalized model '...' to '...' for
        anthropic.
        <reply>

    Strategy: filter out known banner / metadata lines, capture
    session_id from whichever marker produced it (or leave empty),
    concatenate the rest as the reply. Robust to hermes changing
    which metadata it emits across versions / turn types.
    """
    session_id = ""
    content: list[str] = []
    for line in stdout_text.splitlines():
        m = _HERMES_SESSION_ID_RE.match(line)
        if m:
            session_id = m.group(1)
            continue
        m = _HERMES_RESUMED_SESSION_RE.match(line)
        if m:
            session_id = session_id or m.group(1)
            continue
        if _HERMES_MODEL_NORMALISED_RE.match(line):
            continue
        if _HERMES_MODEL_NORMALISED_TAIL_RE.match(line):
            continue
        content.append(line)
    reply = "\n".join(content).strip()
    return reply, session_id


def _puffo_gemini_mcp_entry(
    *,
    agent_id: str,
    mattermost_url: str,
    mattermost_token: str,
    team: str,
    owner_username: str,
) -> dict | None:
    """Build the settings.json entry gemini-cli needs to spawn the
    puffo MCP server. Shape matches gemini's ``mcpServers`` schema
    (command + args + env). Returns ``None`` when we don't have
    the Mattermost pair — the MCP is useless without it, and
    emitting an entry that can't reach the server would surface a
    confusing error to the agent.
    """
    if not (mattermost_url and mattermost_token):
        return None
    env = mcp_env(
        agent_id=agent_id,
        url=mattermost_url,
        token=mattermost_token,
        workspace="/workspace",
        team=team,
        owner_username=owner_username,
        runtime_kind="cli-docker",
        harness="gemini-cli",
    )
    return {
        "command": "python3",
        "args": ["/opt/puffoagent-mcp/puffo_tools.py"],
        "env": env,
    }


def _build_gemini_argv(
    *,
    container_name: str,
    api_key: str,
    model: str,
    has_prior_session: bool,
    user_message: str,
) -> list[str]:
    """Assemble the ``docker exec ... gemini ...`` argv for one turn.

    Extracted so the dash-leading-value invariant is directly
    testable: our turn preamble lines start with ``- `` (markdown
    list syntax) and yargs treats a separate argv that begins with
    ``-`` as another flag. Using ``--prompt=<value>`` instead of
    ``-p <value>`` keeps the whole prompt in a single argv token
    and forces yargs to read everything after ``=`` as the option's
    value — dashes, newlines, CJK, and all.
    """
    cmd = [
        "docker", "exec", "-i",
        "-e", f"GEMINI_API_KEY={api_key}",
        container_name,
        "gemini",
    ]
    if model:
        cmd.extend(["--model", _gemini_model_id(model)])
    if has_prior_session:
        cmd.extend(["-r", "latest"])
    cmd.extend([
        "--output-format", "json",
        f"--prompt={user_message}",
    ])
    return cmd


def _gemini_model_id(model: str) -> str:
    """Translate the agent's ``runtime.model`` into the form
    ``gemini --model`` expects.

    Gemini model ids (``gemini-2.5-pro``, ``gemini-2.5-flash``)
    don't use the provider-prefix shape hermes wants; we just pass
    through whatever the operator set. Claude-style ``[1m]``
    suffixes aren't meaningful here but we strip them anyway to be
    forgiving of copy-paste from other harnesses. Empty / missing
    → a sensible default.
    """
    base = (model or "").split("[", 1)[0].strip()
    if not base:
        return "gemini-2.5-pro"
    return base


def _parse_gemini_reply(stdout_text: str) -> tuple[str, str, str]:
    """Pull the reply / session id / error from ``gemini -p ...
    --output-format json`` stdout.

    Shape observed on gemini-cli 0.38.2::

        {
          "session_id": "<uuid>",
          "response": "<assistant text>",
          "stats": {...big nested model/tool/file usage block...}
        }

    On structured failure the ``response`` is missing and a nested
    ``error`` object takes its place::

        {
          "session_id": "<uuid>",
          "error": {"type": "Error", "message": "...", "code": 1}
        }

    Falls back to the raw stdout text when JSON parse fails — a few
    upstream failure modes print plain text despite ``--output-format
    json``. If the raw text is gemini's help banner (``Usage:
    gemini``) we explicitly return an error rather than leaking the
    banner as the agent's reply; that situation typically means we
    sent a malformed argv.
    """
    stdout_text = stdout_text.strip()
    if not stdout_text:
        return "", "", ""
    try:
        obj = json.loads(stdout_text)
    except (json.JSONDecodeError, ValueError):
        if stdout_text.startswith("Usage: gemini"):
            return "", "", "gemini printed its --help banner instead of a reply; argv likely malformed"
        return stdout_text, "", ""
    if not isinstance(obj, dict):
        return stdout_text, "", ""
    reply = str(obj.get("response", "") or "")
    session_id = str(obj.get("session_id", "") or "")
    err_raw = obj.get("error")
    if isinstance(err_raw, dict):
        err = str(err_raw.get("message", "") or err_raw.get("type", "") or "unknown error")
    else:
        err = str(err_raw or "")
    return reply.strip(), session_id, err


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
