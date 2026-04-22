"""Regression tests for the 2026-04-21 Core 3 freeze incident.

Three failure modes, each with a test:

  1. ``ClaudeSession._one_turn`` reads a single stream-json line
     larger than asyncio's default 64 KiB StreamReader buffer.
     Without the widened ``limit=`` on ``create_subprocess_exec`` this
     raises ``LimitOverrunError`` / ``ValueError`` and the turn wedges.

  2. When ``readline()`` DOES raise (simulating any future overflow
     or protocol corruption), ``_one_turn`` must recover: return a
     silent empty reply, audit the event, and kill the subprocess
     so the next turn respawns — rather than bubble the exception
     and leave the agent stuck in "thinking" state.

  3. The refresh-ping one-shot must detect a 401 / authentication
     error in claude's output, flip ``adapter.auth_healthy`` to
     ``False``, and log at ERROR rather than silently warning.

The first two tests drive ``ClaudeSession`` through a fake proc so
we can shape stdout bytes exactly; the third mocks
``asyncio.create_subprocess_exec`` in the local_cli adapter.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from puffoagent.agent.adapters.cli_session import (
    STREAM_READER_LIMIT_BYTES,
    AuditLog,
    ClaudeSession,
)


# ── Fake subprocess helpers ──────────────────────────────────────────────────


class _FakeStdin:
    def __init__(self):
        self.buffer = bytearray()
        self._closed = False

    def write(self, data: bytes) -> None:
        self.buffer.extend(data)

    async def drain(self) -> None:
        return None

    def is_closing(self) -> bool:
        return self._closed

    def close(self) -> None:
        self._closed = True


class _RaisingReader:
    """Stand-in for ``proc.stdout`` that raises on ``readline`` — used
    to drive the overflow-recovery path deterministically without
    having to construct a real >16 MiB line.
    """
    def __init__(self, exc: BaseException):
        self._exc = exc

    async def readline(self) -> bytes:
        raise self._exc


class _FakeProc:
    """Minimal stand-in for ``asyncio.subprocess.Process`` suitable
    for driving ``_one_turn`` / ``_read_init``.

    StreamReader needs a running loop to construct (binds to
    ``events.get_event_loop()``), so build this instance inside the
    async helper the test runs under — not at module scope.
    """
    def __init__(
        self,
        stdout_lines: list[bytes] | None = None,
        stdout_raises: BaseException | None = None,
        returncode: int = 0,
    ):
        self.stdin = _FakeStdin()
        if stdout_raises is not None:
            self.stdout = _RaisingReader(stdout_raises)
        else:
            reader = asyncio.StreamReader(limit=STREAM_READER_LIMIT_BYTES)
            for line in stdout_lines or []:
                reader.feed_data(line)
            reader.feed_eof()
            self.stdout = reader
        empty = asyncio.StreamReader()
        empty.feed_eof()
        self.stderr = empty
        self.returncode: int | None = None
        self._final_rc = returncode
        self._terminated = False
        self._killed = False

    async def wait(self) -> int:
        self.returncode = self._final_rc
        return self._final_rc

    def terminate(self) -> None:
        self._terminated = True

    def kill(self) -> None:
        self._killed = True


def _make_session(tmp_path: Path, audit: bool = True) -> ClaudeSession:
    """Build a ClaudeSession pointed at tmp_path. ``build_command``
    is unused in the tests (we never hit ``_ensure_running``; we
    inject ``_proc`` directly).
    """
    session_file = tmp_path / "session.json"
    audit_log = AuditLog(tmp_path / "audit.log", agent_id="test-agent") if audit else None
    return ClaudeSession(
        agent_id="test-agent",
        session_file=session_file,
        build_command=lambda args: ["true"],
        cwd=str(tmp_path),
        env={},
        audit=audit_log,
    )


def _read_audit_events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


# ── Test 1: big line ─────────────────────────────────────────────────────────


def test_one_turn_reads_line_larger_than_default_asyncio_limit(tmp_path):
    """A single stream-json result event larger than the old default
    64 KiB buffer must be read successfully. This is the direct
    regression guard for the incident root cause.
    """
    # Build an assistant text block + result event pair, where the
    # assistant text is ~200 KiB (comfortably over the 64 KiB default
    # but well under our 16 MiB limit).
    big_text = "x" * (200 * 1024)
    assistant = {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": big_text}]},
    }
    result = {
        "type": "result",
        "subtype": "success",
        "session_id": "sess-1",
        "usage": {"input_tokens": 10, "output_tokens": 20},
    }
    lines = [
        (json.dumps(assistant) + "\n").encode("utf-8"),
        (json.dumps(result) + "\n").encode("utf-8"),
    ]
    session = _make_session(tmp_path, audit=False)

    async def drive():
        session._proc = _FakeProc(stdout_lines=lines)
        return await session._one_turn("hello")

    out = asyncio.run(drive())
    assert out.reply == big_text
    assert out.input_tokens == 10
    assert out.output_tokens == 20


# ── Test 2: stream overflow recovery ─────────────────────────────────────────


def test_one_turn_recovers_on_readline_overflow(tmp_path, caplog):
    """When the stream reader blows past even our widened limit (or
    sees any protocol corruption surfaced as ValueError), the turn
    must return a silent empty reply, audit the stream_error event,
    and kill the subprocess so the next turn will respawn.
    """
    overflow = ValueError("Separator is not found, and chunk exceed the limit")
    session = _make_session(tmp_path)

    async def drive():
        session._proc = _FakeProc(stdout_raises=overflow, returncode=137)
        return await session._one_turn("hello")

    with caplog.at_level(logging.ERROR):
        out = asyncio.run(drive())

    # Controlled failure: empty reply so the worker doesn't post
    # anything user-visible; metadata flag for the worker to surface.
    assert out.reply == ""
    assert out.metadata.get("stream_error") == "readline_limit"
    assert out.input_tokens == 0

    # Subprocess killed: _kill_proc sets self._proc to None.
    assert session._proc is None

    # Audit log captured the event with action=respawned_claude_subprocess.
    events = _read_audit_events(tmp_path / "audit.log")
    stream_errors = [e for e in events if e.get("event") == "session.stream_error"]
    assert len(stream_errors) == 1
    assert stream_errors[0]["phase"] == "readline_limit"
    assert stream_errors[0]["action"] == "respawned_claude_subprocess"

    # ERROR-level log so operators notice without tailing DEBUG.
    assert any(
        "stream failure" in r.message and r.levelno == logging.ERROR
        for r in caplog.records
    ), "expected an ERROR log from _handle_stream_failure"


def test_one_turn_recovers_on_eof_mid_turn(tmp_path):
    """Subprocess dies mid-turn (stdout returns empty bytes before a
    result event). Same recovery contract as readline-overflow:
    silent empty reply, metadata flag, audit, and kill.
    """
    # Feed no lines so the first readline() returns b'' (EOF).
    session = _make_session(tmp_path)

    async def drive():
        session._proc = _FakeProc(stdout_lines=[], returncode=1)
        return await session._one_turn("hello")

    out = asyncio.run(drive())
    assert out.reply == ""
    assert out.metadata.get("stream_error") == "eof_mid_turn"
    assert session._proc is None

    events = _read_audit_events(tmp_path / "audit.log")
    assert any(
        e.get("event") == "session.stream_error"
        and e.get("phase") == "eof_mid_turn"
        for e in events
    )


# ── Test 3: auth 401 smoke test ──────────────────────────────────────────────


def test_refresh_oneshot_flags_auth_failure(tmp_path, caplog):
    """The refresh-ping one-shot doubles as an inference smoke test.
    A 401 / authentication_error response must flip ``auth_healthy``
    to ``False`` and log at ERROR — ``claude auth status`` reporting
    logged-in was not enough during the 2026-04-21 incident.
    """
    from puffoagent.agent.adapters.local_cli import LocalCLIAdapter

    adapter = LocalCLIAdapter(
        agent_id="smoke-agent",
        model="claude-opus-4-6",
        workspace_dir=str(tmp_path / "ws"),
        claude_dir=str(tmp_path / "ws" / ".claude"),
        session_file=str(tmp_path / "a" / "cli_session.json"),
        mcp_config_file=str(tmp_path / "a" / "mcp-config.json"),
        agent_home_dir=str(tmp_path / "home"),
        permission_mode="default",
    )
    adapter._verified = True  # skip the shutil.which("claude") check
    # Default from Adapter base — probe hasn't run yet.
    assert adapter.auth_healthy is None

    # Fake subprocess returns rc=1 with 401 on stderr, mirroring the
    # output pattern captured in the incident report.
    err_bytes = (
        b'Failed to authenticate. API Error: 401 '
        b'{"type":"error","error":{"type":"authentication_error",'
        b'"message":"Invalid authentication credentials"}}\n'
    )

    class _OneShotProc:
        returncode = 1
        async def communicate(self):
            return b"", err_bytes

    async def fake_exec(*args, **kwargs):
        return _OneShotProc()

    with patch("asyncio.create_subprocess_exec", fake_exec), \
         caplog.at_level(logging.ERROR):
        asyncio.run(adapter._run_refresh_oneshot())

    assert adapter.auth_healthy is False, \
        "auth_healthy should flip to False on 401"
    assert any(
        "auth failure" in r.message and r.levelno == logging.ERROR
        for r in caplog.records
    ), "expected an ERROR log naming the auth failure"


def test_refresh_oneshot_sets_auth_healthy_on_success(tmp_path):
    """Happy-path rc=0 with no auth markers flips auth_healthy to
    True so the operator sees ``health=ok`` once the first probe
    succeeds after startup.
    """
    from puffoagent.agent.adapters.local_cli import LocalCLIAdapter

    adapter = LocalCLIAdapter(
        agent_id="smoke-agent",
        model="claude-opus-4-6",
        workspace_dir=str(tmp_path / "ws"),
        claude_dir=str(tmp_path / "ws" / ".claude"),
        session_file=str(tmp_path / "a" / "cli_session.json"),
        mcp_config_file=str(tmp_path / "a" / "mcp-config.json"),
        agent_home_dir=str(tmp_path / "home"),
        permission_mode="default",
    )
    adapter._verified = True

    # Minimal stream-json result event on stdout — no auth error.
    ok_stdout = (
        b'{"type":"result","subtype":"success","session_id":"s1",'
        b'"usage":{"input_tokens":1,"output_tokens":1}}\n'
    )

    class _OneShotProc:
        returncode = 0
        async def communicate(self):
            return ok_stdout, b""

    async def fake_exec(*args, **kwargs):
        return _OneShotProc()

    with patch("asyncio.create_subprocess_exec", fake_exec):
        asyncio.run(adapter._run_refresh_oneshot())

    assert adapter.auth_healthy is True
