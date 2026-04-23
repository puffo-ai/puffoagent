"""Long-lived ``claude`` CLI session with audit logging.

Spawned once per agent, fed one user message per turn, kept alive
across turns. The ``claude`` process speaks newline-delimited JSON on
both stdin (``--input-format stream-json``) and stdout
(``--output-format stream-json``). The session id from the init event
is persisted to ``cli_session.json`` so a daemon restart can re-spawn
the process with ``--resume <id>`` and continue the same conversation.

This class is agnostic to whether the subprocess runs on the host
(cli-local) or via ``docker exec`` (cli-docker); the caller passes a
``build_command`` callback that returns the full argv. The only
contract is that the argv, when run, launches the claude CLI with
stream-json I/O so the protocol below applies.

Wire protocol summary (see Claude Code docs for full schema):

  stdin (we write)
    {"type":"user","message":{"role":"user","content":"..."},
     "parent_tool_use_id":null,"session_id":"..."}

  stdout (we read), one JSON object per line:
    {"type":"system","subtype":"init","session_id":"...","model":"...","tools":[...]}
    {"type":"assistant","message":{"content":[{"type":"text","text":"..."}, ...]}}
    {"type":"user","message":{"content":[{"type":"tool_result",...}]}}   # tool loop
    {"type":"result","subtype":"success","session_id":"...","usage":{...}}

One turn = write one user event, read until we see a ``result`` event.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .base import TurnResult

logger = logging.getLogger(__name__)


# Cap any single audit field at this many characters so one huge user
# message or tool input doesn't bloat the log or the docker-logs
# stream. Truncation is marked with `... (truncated)`.
AUDIT_FIELD_MAX = 2000


# Substrings (case-insensitive) that indicate the claude subprocess
# emitted an auth / token failure as its reply instead of a real
# response. Kept STRONG-ONLY: every marker is text the model has no
# reason to produce in a normal answer. Weak markers like "401",
# "oauth", "unauthorized" were dropped because users discussing HTTP
# / auth concepts would otherwise tip the retry loop into a 45s
# stall on a perfectly valid reply.
_AUTH_ERROR_MARKERS = (
    "please run /login",
    "please run `claude /login`",
    "run `claude login`",
    "invalid api key",
    "invalid_grant",
    "authentication failed",
    "credentials expired",
    # Patterns observed in the 2026-04-21 Core 3 freeze incident,
    # where stale OAuth state made claude emit raw API errors as
    # its reply. Added here so the retry loop catches them AND so
    # retry exhaustion results in a silent reply rather than
    # "Failed to authenticate. API Error: 401..." leaking into
    # a public channel.
    "failed to authenticate",
    "api error: 401",
    "invalid authentication credentials",
    '"type":"authentication_error"',
)

# Backoffs between retries when an auth-error reply is detected.
# 5 total attempts (initial + 4 retries) for a worst case of ~45s
# of waiting. The first interval is short on purpose: the most
# common cause is a multi-agent rotating-refresh-token race that
# resolves within a second of the winner writing the new token to
# the shared `.credentials.json`.
AUTH_RETRY_BACKOFFS_SECONDS = (3, 6, 12, 24)


def _looks_like_auth_error(text: str) -> bool:
    if not text:
        return False
    low = text.lower()
    return any(marker in low for marker in _AUTH_ERROR_MARKERS)


class AuditLog:
    """Per-agent ndjson audit log.

    Every line is one event (session.start, turn.input, tool,
    assistant.text, turn.end, session.error, ...). Living inside the
    agent's workspace is intentional — the workspace is bind-mounted
    into the cli-docker container, so the same file feeds the
    container's ``tail -F`` PID 1 and ``docker logs``.
    """

    def __init__(self, path: Path, agent_id: str):
        self.path = path
        self.agent_id = agent_id
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Ensure the file exists so the container's tail starts
            # cleanly even if no turn has happened yet.
            self.path.touch(exist_ok=True)
        except OSError as exc:
            logger.warning(
                "agent %s: cannot prepare audit log at %s: %s",
                agent_id, path, exc,
            )

    def write(self, event: str, **fields) -> None:
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "agent": self.agent_id,
            "event": event,
            **{k: _truncate(v) for k, v in fields.items()},
        }
        try:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError as exc:
            # Audit failure must not kill the turn.
            logger.warning(
                "agent %s: audit log write failed: %s",
                self.agent_id, exc,
            )


def _truncate(v):
    if isinstance(v, str) and len(v) > AUDIT_FIELD_MAX:
        return v[:AUDIT_FIELD_MAX] + "... (truncated)"
    if isinstance(v, dict):
        return {k: _truncate(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_truncate(x) for x in v]
    return v


# Seconds to wait for the init event after spawn before giving up.
# Some claude versions delay init until the first user message; we
# don't block forever so that "first turn on a cold session" still
# works even if init arrives interleaved with the first result.
INIT_TIMEOUT_SECONDS = 10.0


# Buffer size for the asyncio StreamReader wrapping the claude
# subprocess's stdout. The asyncio default is 64 KiB — one
# newline-delimited stream-json event emitted by Opus-class models
# (verbose metadata + long tool results + large assistant blocks)
# routinely exceeds that, and when it does ``readline()`` raises
# ``LimitOverrunError``, the turn wedges, and the UI shows the
# agent "still thinking" indefinitely. See the 2026-04-21 Core 3
# freeze incident report for the failure this prevents. 16 MiB is
# comfortably larger than any single event we've seen in the wild
# while still bounding memory per agent.
STREAM_READER_LIMIT_BYTES = 16 * 1024 * 1024


class _ResumeFailed(Exception):
    """The subprocess exited before emitting an init event — almost
    always because ``--resume <id>`` was passed with a stale session
    id that claude no longer has a transcript for."""


class ClaudeSession:
    def __init__(
        self,
        agent_id: str,
        session_file: Path,
        build_command: Callable[[list[str]], list[str]],
        cwd: Optional[str] = None,
        env: Optional[dict[str, str]] = None,
        audit: Optional["AuditLog"] = None,
        extra_args: Optional[list[str]] = None,
    ):
        """
        ``build_command(extra_args)`` is called with a list of extra
        claude flags (e.g. ``["--resume", "abc"]``) and must return
        the full argv list to spawn. For cli-local this prepends
        ``["claude", "--dangerously-skip-permissions", ...]``; for
        cli-docker it prepends ``["docker", "exec", "-i", name,
        "claude", "--dangerously-skip-permissions", ...]``.

        ``audit`` is optional. When provided, each turn appends
        structured events for operators to tail.
        """
        self.agent_id = agent_id
        self.session_file = session_file
        self.build_command = build_command
        self.cwd = cwd
        self.env = env
        self.audit = audit
        # Extra claude-code CLI args injected every spawn — most
        # commonly --mcp-config <path> and --permission-prompt-tool.
        # Re-applied on every respawn (including after --resume
        # fallback) so the puffo tools stay available for the whole
        # agent lifetime.
        self.extra_args = list(extra_args or [])

        self._proc: asyncio.subprocess.Process | None = None
        self._system_prompt_seen: str | None = None
        self._session_id: str = self._load_session_id()
        self._lock = asyncio.Lock()
        self._stderr_drain_task: asyncio.Task | None = None

    # ── Public API ────────────────────────────────────────────────────────────

    async def run_turn(self, user_message: str, system_prompt: str) -> TurnResult:
        async with self._lock:
            await self._ensure_running(system_prompt)
            # Retry loop for auth-error replies. The most common
            # cause is a transient — multi-agent rotating-refresh-
            # token race or a 5xx blip on Anthropic's auth path —
            # so a few short backoffs almost always rescue the turn
            # without the user ever seeing a degraded reply.
            attempts = len(AUTH_RETRY_BACKOFFS_SECONDS) + 1
            last_result: TurnResult | None = None
            for attempt in range(attempts):
                if attempt > 0:
                    delay = AUTH_RETRY_BACKOFFS_SECONDS[attempt - 1]
                    logger.warning(
                        "agent %s: auth-error reply on attempt %d/%d; "
                        "retrying in %ds",
                        self.agent_id, attempt, attempts, delay,
                    )
                    await asyncio.sleep(delay)
                    # Subprocess may have died during the wait (e.g.
                    # crash from the auth failure). Re-ensure running
                    # so the retry has somewhere to go; on respawn
                    # claude re-reads the shared credentials file.
                    await self._ensure_running(system_prompt)
                result = await self._one_turn(user_message)
                if not _looks_like_auth_error(result.reply):
                    return result
                last_result = result
                if self.audit is not None:
                    self.audit.write(
                        "auth_error.detected",
                        attempt=attempt + 1,
                        of=attempts,
                        reply=result.reply,
                    )
            # All attempts exhausted. We used to surface a visible
            # "Agent Token Refreshing Needed" reply, but that leaked
            # operator-speak into public channels (see 2026-04-21
            # freeze incident report). Keep the detailed error on the
            # operator side — ERROR log + audit + metadata — and
            # return an empty reply so ``core.handle_message`` maps
            # it to "don't post". The operator still sees the state
            # via logs and via runtime health (set by the worker
            # from ``metadata['auth_failed']``).
            logger.error(
                "agent %s: auth error persisted across %d attempts; "
                "suppressing reply. last reply: %s",
                self.agent_id, attempts,
                (last_result.reply if last_result else "")[:500],
            )
            if self.audit is not None:
                self.audit.write(
                    "auth_error.exhausted_retries",
                    attempts=attempts,
                    reply=last_result.reply if last_result else "",
                )
            md: dict = {"auth_failed": True, "attempts": attempts}
            if last_result is not None:
                md = {**last_result.metadata, **md}
                return TurnResult(
                    reply="",
                    input_tokens=last_result.input_tokens,
                    output_tokens=last_result.output_tokens,
                    tool_calls=last_result.tool_calls,
                    metadata=md,
                )
            return TurnResult(reply="", metadata=md)

    async def warm(self, system_prompt: str) -> None:
        """Spawn the claude subprocess without running a turn so the
        first real message doesn't wait ~15s for process + init.
        Idempotent: safe to call even if the process is already
        running.
        """
        async with self._lock:
            await self._ensure_running(system_prompt)

    def has_persisted_session(self) -> bool:
        """True when we have a saved session id from a previous run
        — i.e. eager warming would resume an existing conversation
        rather than just paying startup cost for an idle agent."""
        return bool(self._session_id)

    async def aclose(self) -> None:
        async with self._lock:
            await self._kill_proc()

    # ── Session id persistence ────────────────────────────────────────────────

    def _load_session_id(self) -> str:
        if not self.session_file.exists():
            return ""
        try:
            data = json.loads(self.session_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ""
        return (data.get("session_id") or "").strip()

    def _save_session_id(self, sid: str) -> None:
        self._session_id = sid
        data = {"session_id": sid, "updated_at": int(time.time())}
        self.session_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.session_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(self.session_file)

    def _clear_session_id(self) -> None:
        self._session_id = ""
        try:
            self.session_file.unlink()
        except OSError:
            pass

    # ── Subprocess lifecycle ──────────────────────────────────────────────────

    async def _ensure_running(self, system_prompt: str) -> None:
        if self._proc is not None and self._proc.returncode is None:
            return
        if self._proc is not None:
            logger.warning(
                "agent %s: claude subprocess exited (rc=%s); re-spawning",
                self.agent_id, self._proc.returncode,
            )
            self._proc = None

        try:
            await self._spawn(system_prompt)
            return
        except _ResumeFailed as exc:
            logger.warning(
                "agent %s: --resume failed (%s); starting a fresh session",
                self.agent_id, exc,
            )
            self._clear_session_id()
            await self._spawn(system_prompt)

    async def _spawn(self, system_prompt: str) -> None:
        # --verbose is required whenever --output-format stream-json is
        # combined with --print / streaming input. Claude CLI rejects
        # the combo otherwise with:
        #   "When using --print, --output-format=stream-json requires --verbose"
        args = [
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose",
        ]
        args.extend(self.extra_args)
        # The role / system prompt is NOT passed on argv. The worker
        # writes it (plus shared primer and memory snapshot) to
        # <cwd>/.claude/CLAUDE.md, which Claude Code auto-discovers at
        # startup via its project-level file lookup. We keep the
        # ``system_prompt`` parameter around for symmetry with SDK /
        # chat-only but it's only captured for diagnostics here.
        self._system_prompt_seen = system_prompt or None
        if self._session_id:
            args.extend(["--resume", self._session_id])

        cmd = self.build_command(args)
        logger.info(
            "agent %s: spawning claude session (resume=%s)",
            self.agent_id, bool(self._session_id),
        )
        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.cwd,
            env=self.env,
            limit=STREAM_READER_LIMIT_BYTES,
        )
        if self.audit is not None:
            self.audit.write(
                "session.start",
                resume=bool(self._session_id),
                session_id=self._session_id or "",
            )
        # Try to capture session_id from init. We time out gracefully:
        # if the CLI version delays init, we'll pick the id up from
        # the first result event instead. Stderr is drained ONLY after
        # a successful init so the failure path can read it for
        # diagnostics.
        try:
            sid = await asyncio.wait_for(
                self._read_init(self._proc), timeout=INIT_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.debug(
                "agent %s: no init event within %.1fs; will capture session_id from first result",
                self.agent_id, INIT_TIMEOUT_SECONDS,
            )
            self._stderr_drain_task = asyncio.ensure_future(self._drain_stderr(self._proc))
            return
        if sid and sid != self._session_id:
            self._save_session_id(sid)
        self._stderr_drain_task = asyncio.ensure_future(self._drain_stderr(self._proc))

    async def _read_init(self, proc: asyncio.subprocess.Process) -> str:
        while True:
            line = await proc.stdout.readline()
            if not line:
                rc = await proc.wait()
                # Grab stderr synchronously for the exception message —
                # no drain task is running yet at this point.
                stderr_tail = ""
                if proc.stderr is not None:
                    try:
                        buf = await asyncio.wait_for(proc.stderr.read(), timeout=1.0)
                        stderr_tail = buf.decode("utf-8", errors="replace").strip()[-800:]
                    except asyncio.TimeoutError:
                        pass
                raise _ResumeFailed(
                    f"claude exited rc={rc} before init event"
                    + (f"; stderr: {stderr_tail}" if stderr_tail else "")
                )
            event = _parse_event(line)
            if event is None:
                continue
            if event.get("type") == "system" and event.get("subtype") == "init":
                return (event.get("session_id") or "").strip()

    async def _drain_stderr(self, proc: asyncio.subprocess.Process) -> None:
        if proc.stderr is None:
            return
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    return
                text = line.decode("utf-8", errors="replace").rstrip()
                if not text:
                    continue
                # Surface stderr at WARNING by default — most claude
                # output on stderr is a real complaint worth seeing
                # without bumping the global log level.
                logger.warning(
                    "agent %s claude stderr: %s",
                    self.agent_id, text,
                )
        except Exception:
            return

    async def _handle_stream_failure(self, phase: str, exc) -> None:
        """Shared cleanup when the stream-json protocol goes sideways
        mid-turn (oversize line, broken pipe, subprocess EOF). Logs,
        audits, and kills the subprocess so ``_ensure_running`` will
        respawn a fresh one on the next turn. Callers are expected
        to return a silent empty reply so the shell doesn't post a
        runtime-flavoured message to the channel.
        """
        err_type = type(exc).__name__ if isinstance(exc, BaseException) else "str"
        err_str = str(exc)
        logger.error(
            "agent %s: claude stream failure in %s (%s: %s) — "
            "killing subprocess; next turn will respawn",
            self.agent_id, phase, err_type, err_str,
        )
        if self.audit is not None:
            self.audit.write(
                "session.stream_error",
                phase=phase,
                error_type=err_type,
                error=err_str,
                action="respawned_claude_subprocess",
            )
        await self._kill_proc()

    async def _kill_proc(self) -> None:
        if self._proc is None:
            return
        proc = self._proc
        self._proc = None
        if proc.returncode is not None:
            return
        try:
            if proc.stdin and not proc.stdin.is_closing():
                proc.stdin.close()
        except Exception:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=3.0)
            return
        except asyncio.TimeoutError:
            pass
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=3.0)
            return
        except (asyncio.TimeoutError, ProcessLookupError):
            pass
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass

    # ── One turn ──────────────────────────────────────────────────────────────

    async def _one_turn(self, user_message: str) -> TurnResult:
        assert self._proc is not None and self._proc.stdin is not None
        if self.audit is not None:
            self.audit.write("turn.input", content=user_message)
        turn_started_at = time.time()
        frame = {
            "type": "user",
            "message": {"role": "user", "content": user_message},
            "parent_tool_use_id": None,
            "session_id": self._session_id or "puffoagent-turn",
        }
        self._proc.stdin.write((json.dumps(frame) + "\n").encode("utf-8"))
        try:
            await self._proc.stdin.drain()
        except (ConnectionResetError, BrokenPipeError) as exc:
            # Subprocess died before we could hand it the turn.
            # Handle the same way as a mid-read failure: kill, audit,
            # surface a silent empty reply so the shell doesn't post
            # a runtime error into the channel. Next turn will
            # respawn via _ensure_running.
            await self._handle_stream_failure("stdin_drain", exc)
            return TurnResult(reply="", metadata={"stream_error": "stdin_drain"})

        reply_parts: list[str] = []
        tool_calls = 0
        # Names of every tool invoked this turn. Kept for debug /
        # audit; the shell's double-post decision uses the more
        # precise ``send_message_targets`` below.
        tool_names_used: list[str] = []
        # ``(channel, root_id)`` of every ``mcp__puffo__send_message``
        # call. The shell compares these against the current turn's
        # (channel_id/channel_name, root_id) to decide whether to
        # suppress its auto-reply — otherwise narration text around
        # the MCP call posts as a duplicate in the same conversation
        # slot. Empty ``root_id`` = top-level post in the channel.
        send_message_targets: list[dict] = []
        input_tokens = 0
        output_tokens = 0
        event_types_seen: list[str] = []

        while True:
            try:
                line = await self._proc.stdout.readline()
            except (asyncio.LimitOverrunError, ValueError) as exc:
                # asyncio wraps LimitOverrunError in ValueError when
                # raising out of readline(); catch both. This is the
                # exact path the 2026-04-21 Core 3 freeze incident
                # hit — a single stream-json event larger than the
                # StreamReader buffer. We widened the buffer to 16
                # MiB at spawn time, but if a future event still
                # exceeds that we recover rather than wedge.
                await self._handle_stream_failure("readline_limit", exc)
                return TurnResult(reply="", metadata={"stream_error": "readline_limit"})
            except (ConnectionResetError, BrokenPipeError) as exc:
                await self._handle_stream_failure("readline_pipe", exc)
                return TurnResult(reply="", metadata={"stream_error": "readline_pipe"})
            if not line:
                rc = await self._proc.wait()
                # Subprocess died mid-turn. Historically we raised and
                # let the worker surface a generic handle_message
                # error; now we audit, trigger respawn on next turn,
                # and return a silent empty reply so users don't see
                # a traceback-flavoured bot message.
                await self._handle_stream_failure("eof_mid_turn", f"rc={rc}")
                return TurnResult(reply="", metadata={"stream_error": "eof_mid_turn"})
            event = _parse_event(line)
            if event is None:
                continue
            event_types_seen.append(
                f"{event.get('type')}/{event.get('subtype', '-')}"
            )
            logger.debug("agent %s stream event: %s", self.agent_id, event)

            t = event.get("type")
            if t == "assistant":
                msg = event.get("message") or {}
                for block in msg.get("content") or []:
                    if not isinstance(block, dict):
                        continue
                    bt = block.get("type")
                    if bt == "text":
                        text = block.get("text", "") or ""
                        reply_parts.append(text)
                        if self.audit is not None and text:
                            self.audit.write("assistant.text", text=text)
                    elif bt == "tool_use":
                        tool_calls += 1
                        name = block.get("name", "")
                        tool_input = block.get("input") or {}
                        tool_names_used.append(name)
                        if name == "mcp__puffo__send_message":
                            send_message_targets.append({
                                "channel": str(tool_input.get("channel", "")),
                                "root_id": str(tool_input.get("root_id", "")),
                            })
                        if self.audit is not None:
                            self.audit.write(
                                "tool",
                                name=name,
                                input=tool_input,
                                id=block.get("id", ""),
                            )
            elif t == "system":
                sid = (event.get("session_id") or "").strip()
                if sid and sid != self._session_id:
                    self._save_session_id(sid)
            elif t == "result":
                sid = (event.get("session_id") or "").strip()
                if sid and sid != self._session_id:
                    self._save_session_id(sid)
                usage = event.get("usage") or {}
                input_tokens = int(usage.get("input_tokens", 0) or 0)
                output_tokens = int(usage.get("output_tokens", 0) or 0)
                # Fallback: result.result carries the full assembled
                # text reply in some CLI versions. If our
                # AssistantMessage text-block extraction came up
                # empty, use it.
                result_text = event.get("result") or ""
                if not reply_parts and result_text:
                    reply_parts.append(result_text)
                break

        reply = "".join(reply_parts).strip()
        if not reply:
            logger.warning(
                "agent %s: claude turn produced no text reply. events seen: %s",
                self.agent_id, event_types_seen,
            )
        # Auth-error detection + rewriting lives in run_turn so the
        # retry loop can see the raw reply per attempt; _one_turn
        # only owns "run one turn, report what happened".
        if self.audit is not None:
            self.audit.write(
                "turn.end",
                reply_len=len(reply),
                tool_calls=tool_calls,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                duration_ms=int((time.time() - turn_started_at) * 1000),
                event_types=event_types_seen,
            )
        return TurnResult(
            reply=reply,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            tool_calls=tool_calls,
            metadata={
                "session_id": self._session_id,
                "tool_names": tool_names_used,
                "send_message_targets": send_message_targets,
            },
        )


def _parse_event(line: bytes) -> dict | None:
    try:
        return json.loads(line.decode("utf-8").strip())
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
