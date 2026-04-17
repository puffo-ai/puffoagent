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

        self._proc: asyncio.subprocess.Process | None = None
        self._system_prompt_seen: str | None = None
        self._session_id: str = self._load_session_id()
        self._lock = asyncio.Lock()
        self._stderr_drain_task: asyncio.Task | None = None

    # ── Public API ────────────────────────────────────────────────────────────

    async def run_turn(self, user_message: str, system_prompt: str) -> TurnResult:
        async with self._lock:
            await self._ensure_running(system_prompt)
            return await self._one_turn(user_message)

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
        if system_prompt:
            args.extend(["--append-system-prompt", system_prompt])
            self._system_prompt_seen = system_prompt
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
            raise RuntimeError(
                f"agent {self.agent_id}: claude subprocess died before we could "
                f"send the turn ({exc})"
            ) from exc

        reply_parts: list[str] = []
        tool_calls = 0
        input_tokens = 0
        output_tokens = 0
        event_types_seen: list[str] = []

        while True:
            line = await self._proc.stdout.readline()
            if not line:
                rc = await self._proc.wait()
                raise RuntimeError(
                    f"agent {self.agent_id}: claude subprocess died mid-turn (rc={rc})"
                )
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
                        if self.audit is not None:
                            self.audit.write(
                                "tool",
                                name=block.get("name", ""),
                                input=block.get("input") or {},
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
            metadata={"session_id": self._session_id},
        )


def _parse_event(line: bytes) -> dict | None:
    try:
        return json.loads(line.decode("utf-8").strip())
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
