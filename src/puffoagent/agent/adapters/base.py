"""Adapter interface.

An adapter is a thin translation layer between the portal and an
external agent runtime. The runtime (the claude-agent-sdk package, the
claude CLI, etc.) owns the agentic loop and the tool catalog; the
adapter just translates ``TurnContext`` into the runtime's native
invocation, forwards its output back as a ``TurnResult``, and manages
the runtime instance's lifecycle.

See ``DESIGN.md`` at the repo root for the full responsibility split.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional


logger = logging.getLogger(__name__)


ProgressCallback = Callable[[str], Awaitable[None]]


# Refresh when the access token has fewer than this many seconds
# remaining. Token TTL varies by plan (Pro ~1h, Max ~8h), so we
# key off the absolute ``expiresAt`` field in .credentials.json
# rather than a mtime-based heuristic.
#
# Empirically, Anthropic's OAuth endpoint **refuses to rotate a
# token that's more than ~10 min from expiry** — it returns the
# existing token unchanged. Running the refresh one-shot any
# earlier than that burns an API call and logs a false-positive
# "expiry didn't advance" warning. Setting the threshold to 5 min
# lands every refresh attempt safely inside Anthropic's accept
# window while still leaving enough headroom for the next worker
# tick (default 10 min) to retry before the token actually dies.
CREDENTIAL_REFRESH_BEFORE_EXPIRY_SECONDS = 5 * 60


# Daemon-wide mutex across every Adapter instance. OAuth uses
# rotating refresh tokens — each refresh invalidates the previous
# refresh_token, so two agents that both call the refresh endpoint
# at the same time will see one win and the other get
# ``invalid_grant``. With the shared-credentials bind-mount
# (cli-docker) or per-agent copies seeded from the host (cli-
# local), the race is real any time more than one agent ticks past
# the expiry threshold in the same moment.
#
# Policy: the first agent to grab the lock does the refresh. Late
# arrivals find the lock held and SKIP entirely (not queue) —
# their next tick will see the freshly-refreshed file and skip
# naturally.
#
# asyncio.Lock is safe to construct at module-load time in Python
# 3.10+ (no loop binding).
_REFRESH_LOCK = asyncio.Lock()


@dataclass
class TurnContext:
    """Everything an adapter needs to run one turn of conversation.

    ``workspace_dir`` / ``claude_dir`` / ``memory_dir`` are absolute
    paths the adapter may bind-mount or pass to its runtime. Not every
    adapter uses every field — chat-only adapters ignore the directory
    fields entirely.
    """
    system_prompt: str
    messages: list[dict]
    workspace_dir: str = ""
    claude_dir: str = ""
    memory_dir: str = ""
    on_progress: Optional[ProgressCallback] = None


@dataclass
class TurnResult:
    """Everything the shell needs back from an adapter to finish a turn.

    ``reply`` of ``""`` or ``"[SILENT]"`` means the agent chose not to
    respond; the shell translates both to "don't post to Mattermost".
    """
    reply: str
    input_tokens: int = 0
    output_tokens: int = 0
    tool_calls: int = 0
    metadata: dict = field(default_factory=dict)


class Adapter(ABC):
    """Base class for all runtime adapters."""

    # Observed auth/inference health from the most recent probe
    # (refresh ping or smoke test). ``None`` = never checked, ``True``
    # = last probe succeeded, ``False`` = last probe found an auth
    # failure (claude responded with a 401 / authentication_error).
    # Set by the refresh_ping path; read by the worker to surface
    # an ``auth_failed`` sub-status in ``puffoagent status``.
    # Default attribute rather than __init__ field so every subclass
    # inherits it without touching its constructor.
    auth_healthy: bool | None = None

    @abstractmethod
    async def run_turn(self, ctx: TurnContext) -> TurnResult:
        """Execute one turn against the underlying runtime."""

    async def warm(self, system_prompt: str) -> None:
        """Pre-spawn any long-lived runtime state so the first turn
        doesn't pay startup latency. The worker calls this right after
        construction if the agent already has a persisted session (so
        it can ``--resume`` immediately on daemon start).

        Default is a no-op for stateless adapters (chat-only, sdk).
        """
        return None

    async def reload(self, new_system_prompt: str) -> None:
        """Drop any cached claude-subprocess state so the next turn
        picks up fresh content from disk (CLAUDE.md layers, profile,
        memory). The worker calls this between turns when the agent
        triggered a reload via the ``reload_system_prompt`` MCP tool.

        Implementation note: CLI adapters close the long-lived claude
        subprocess (keeping the container alive for cli-docker); the
        next ``run_turn`` spawns a new subprocess that re-reads all
        CLAUDE.md layers on startup. SDK and chat-only adapters pass
        ``system_prompt`` per-turn anyway, so the shell updating its
        own ``system_prompt`` is all that's needed — default no-op
        here is correct for them.
        """
        return None

    async def refresh_ping(self) -> None:
        """Force an auth round-trip so Anthropic's rotating OAuth
        refresh token gets exchanged before the access token dies.
        The worker calls this periodically on every agent.

        Structured as an orchestrator around two subclass hooks:
          - ``_credentials_expires_in_seconds()`` — how long until
            the current access token expires?
          - ``_run_refresh_oneshot()`` — actually do the refresh

        Guarded by a daemon-wide mutex (``_REFRESH_LOCK``) so N
        agents that all tick past the expiry threshold at once
        don't dogpile the refresh endpoint — only the first to
        arrive runs a refresh; the others skip (no-op, not queue).
        Their next tick sees a fresh file and skips naturally.

        SDK / chat-only adapters use static API keys and inherit
        the default ``_credentials_expires_in_seconds`` of
        ``None`` which short-circuits the orchestrator.
        """
        expires_in_before = self._credentials_expires_in_seconds()
        if expires_in_before is None:
            return
        if expires_in_before > CREDENTIAL_REFRESH_BEFORE_EXPIRY_SECONDS:
            logger.debug(
                "credentials fresh (expires in %ds), skipping refresh ping",
                expires_in_before,
            )
            return

        # Don't queue behind an in-flight refresh. If another agent
        # is already doing one, our file will be updated by the time
        # we see it on the next tick.
        if _REFRESH_LOCK.locked():
            logger.debug(
                "another agent is refreshing; skipping this tick "
                "(expires in %ds; next tick will see fresh file)",
                expires_in_before,
            )
            return

        async with _REFRESH_LOCK:
            # Re-check after acquiring — another agent may have
            # finished its refresh just before we got here, in
            # which case the file is already fresh.
            expires_in_recheck = self._credentials_expires_in_seconds()
            if expires_in_recheck is None:
                logger.warning(
                    "refresh_ping: credentials file disappeared "
                    "between threshold check and lock acquire"
                )
                return
            if expires_in_recheck > CREDENTIAL_REFRESH_BEFORE_EXPIRY_SECONDS:
                logger.info(
                    "credentials refreshed by another agent "
                    "(expires in %ds); skipping", expires_in_recheck,
                )
                return

            logger.info(
                "credentials expire in %ds — running refresh ping",
                expires_in_recheck,
            )
            try:
                await self._run_refresh_oneshot()
            except Exception as exc:
                logger.warning("refresh_ping failed: %s", exc)
                return

            expires_in_after = self._credentials_expires_in_seconds()
            if expires_in_after is None:
                logger.warning(
                    "refresh_ping ran but credentials file is no "
                    "longer readable (was expiring in %ds)",
                    expires_in_recheck,
                )
                return
            logger.info(
                "credentials refreshed: expires in %ds (was %ds)",
                expires_in_after, expires_in_recheck,
            )
            if expires_in_after <= expires_in_recheck:
                logger.warning(
                    "refresh_ping ran but token expiry didn't advance "
                    "— claude may not be rewriting the credentials "
                    "file; check OAuth state"
                )

    def _credentials_expires_in_seconds(self) -> int | None:
        """Return seconds until the OAuth access token expires (may
        be negative if already expired), or ``None`` if this
        adapter doesn't use OAuth (SDK / chat-only) or the file
        can't be parsed. Subclass hook used by ``refresh_ping``.
        """
        return None

    async def _run_refresh_oneshot(self) -> None:
        """Spawn a short-lived claude invocation that forces an auth
        round-trip and writes the refreshed token back to
        ``.credentials.json``. Must NOT reuse the adapter's long-
        lived session — the whole point is that the one-shot
        process exits and triggers the credentials-write path.
        Subclass hook used by ``refresh_ping``; default is a no-op
        to keep SDK / chat-only adapters clean.
        """
        return None

    async def aclose(self) -> None:
        """Release any runtime resources (containers, subprocesses, MCP
        servers). Default is a no-op for stateless adapters.
        """
        return None


# Substrings (case-insensitive) that mark a claude CLI one-shot
# output as an auth failure rather than a real reply. Called from
# the refresh-ping / smoke-test path in every CLI adapter. The
# patterns come from the 2026-04-21 Core 3 freeze incident (stale
# OAuth, ``claude auth status`` still reported logged-in but the
# API returned 401). Kept deliberately strong so a user happening
# to ask about HTTP auth doesn't flip the health flag.
_AUTH_FAILURE_SIGNATURES = (
    "api error: 401",
    "invalid authentication credentials",
    '"type":"authentication_error"',
    "authentication_error",
    "invalid_grant",
    "please run /login",
    "please run `claude /login`",
    "run `claude login`",
)


def looks_like_auth_failure(*parts: str) -> bool:
    """True if any of the supplied strings (stdout, stderr, reply
    text) contain a claude auth-failure signature. Case-insensitive.
    """
    for p in parts:
        if not p:
            continue
        low = p.lower()
        if any(sig in low for sig in _AUTH_FAILURE_SIGNATURES):
            return True
    return False


def format_history_as_prompt(messages: list[dict]) -> str:
    """Render shell conversation history as a single prompt string.

    The SDK adapter is one-shot per turn; the two CLI adapters keep a
    long-lived subprocess that owns its own session, so they pass
    only the latest user message (see local_cli / docker_cli). This
    helper is therefore only used by the SDK adapter today, but kept
    in the base module for any future adapter that wants to embed
    history in a single prompt string.
    """
    if not messages:
        return ""
    if len(messages) == 1:
        return messages[0]["content"]
    parts = ["<prior_turns>"]
    for m in messages[:-1]:
        parts.append(f"[{m['role']}]\n{m['content']}")
    parts.append("</prior_turns>")
    parts.append(messages[-1]["content"])
    return "\n\n".join(parts)
