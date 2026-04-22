"""Tests for ``link_host_credentials`` — the shared-credentials
plumbing that makes every cli-local agent track the operator's live
``~/.claude/.credentials.json`` the way cli-docker's bind-mount does.

Contract:
  * Symlink-preferred: when the platform permits, the agent's
    ``.credentials.json`` becomes a symlink to the host's file. Free
    read-through, survives atomic-rename rewrites on the host side.
  * Copy fallback: when ``os.symlink`` raises (Windows without
    Developer Mode, etc.), fall back to a copy. Must be refreshed
    on subsequent calls when the host file changes.
  * Idempotent: repeat calls don't churn the file unless state
    actually changed. Scenarios:
      - already-symlinked-correctly → "symlink (already)"
      - already-copied-and-fresh    → "copy (fresh)"
  * Replaces pre-existing state cleanly (stale copy from the old
    per-agent-copy design, broken symlink from a prior host).
  * Missing host file is a reportable no-op — no file created.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from puffoagent.portal.state import link_host_credentials


# A ToleratesSymlinkAbsence = "we can't assume os.symlink works on
# every CI runner or on Windows w/o Dev Mode". Two knobs:
#   - _force_copy_path: monkeypatch os.symlink to raise so the copy
#     fallback is exercised deterministically.
#   - test_symlink_path runs ONLY when symlinks work here; skipped
#     otherwise so we don't fail on locked-down hosts.


def _symlinks_available(tmp_path: Path) -> bool:
    """Probe: can this process create a symlink in ``tmp_path``?"""
    probe = tmp_path / "_probe_symlink"
    target = tmp_path / "_probe_target"
    target.write_text("x", encoding="utf-8")
    try:
        os.symlink(target, probe)
        probe.unlink()
        target.unlink()
        return True
    except (OSError, NotImplementedError):
        try:
            target.unlink()
        except OSError:
            pass
        return False


def _write_host_creds(host: Path, content: str = '{"claudeAiOauth":{"expiresAt":1}}') -> Path:
    p = host / ".claude" / ".credentials.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


# ── Symlink path ────────────────────────────────────────────────────────────


def test_symlink_created_when_supported(tmp_path):
    if not _symlinks_available(tmp_path):
        pytest.skip("symlinks unavailable on this host")
    host = tmp_path / "host"
    agent = tmp_path / "agent" / "home"
    host_creds = _write_host_creds(host, '{"k":"v1"}')

    mode = link_host_credentials(host, agent)

    assert mode == "symlink"
    agent_creds = agent / ".claude" / ".credentials.json"
    assert agent_creds.is_symlink()
    assert agent_creds.read_text() == '{"k":"v1"}'
    # Host rewrite is visible via the symlink without another link call.
    host_creds.write_text('{"k":"v2"}', encoding="utf-8")
    assert agent_creds.read_text() == '{"k":"v2"}'


def test_symlink_idempotent(tmp_path):
    if not _symlinks_available(tmp_path):
        pytest.skip("symlinks unavailable on this host")
    host = tmp_path / "host"
    agent = tmp_path / "agent"
    _write_host_creds(host)

    assert link_host_credentials(host, agent) == "symlink"
    # Second call: same target, nothing to do.
    assert link_host_credentials(host, agent) == "symlink (already)"


def test_symlink_replaces_stale_regular_file(tmp_path):
    """Pre-existing agents from the old per-agent-copy design have a
    regular file at .credentials.json. The link helper must tear
    that down and put a symlink in its place."""
    if not _symlinks_available(tmp_path):
        pytest.skip("symlinks unavailable on this host")
    host = tmp_path / "host"
    agent = tmp_path / "agent"
    _write_host_creds(host, '{"k":"host"}')
    # Stale regular-file copy (what old seed_claude_home left behind).
    agent_creds = agent / ".claude" / ".credentials.json"
    agent_creds.parent.mkdir(parents=True, exist_ok=True)
    agent_creds.write_text('{"k":"stale-agent-copy"}', encoding="utf-8")

    mode = link_host_credentials(host, agent)

    assert mode == "symlink"
    assert agent_creds.is_symlink()
    assert agent_creds.read_text() == '{"k":"host"}'


def test_symlink_replaces_broken_prior_symlink(tmp_path):
    """A symlink left over from a prior setup that points somewhere
    no longer existing should be replaced, not silently preserved."""
    if not _symlinks_available(tmp_path):
        pytest.skip("symlinks unavailable on this host")
    host = tmp_path / "host"
    agent = tmp_path / "agent"
    _write_host_creds(host, '{"k":"host"}')
    agent_creds = agent / ".claude" / ".credentials.json"
    agent_creds.parent.mkdir(parents=True, exist_ok=True)
    # Broken symlink: target doesn't exist.
    os.symlink(tmp_path / "ghost", agent_creds)
    assert agent_creds.is_symlink()
    assert not agent_creds.exists()

    mode = link_host_credentials(host, agent)

    assert mode == "symlink"
    assert agent_creds.read_text() == '{"k":"host"}'


# ── Copy fallback ───────────────────────────────────────────────────────────


def test_copy_fallback_when_symlink_raises(tmp_path, monkeypatch):
    host = tmp_path / "host"
    agent = tmp_path / "agent"
    _write_host_creds(host, '{"k":"v1"}')

    def _fail_symlink(*a, **kw):
        raise OSError("simulated no-symlink-privilege")
    monkeypatch.setattr(os, "symlink", _fail_symlink)

    mode = link_host_credentials(host, agent)

    assert mode == "copy"
    agent_creds = agent / ".claude" / ".credentials.json"
    assert not agent_creds.is_symlink()
    assert agent_creds.read_text() == '{"k":"v1"}'


def test_copy_fallback_is_idempotent_when_host_unchanged(tmp_path, monkeypatch):
    host = tmp_path / "host"
    agent = tmp_path / "agent"
    _write_host_creds(host, '{"k":"v1"}')
    monkeypatch.setattr(os, "symlink", lambda *a, **k: (_ for _ in ()).throw(OSError()))

    assert link_host_credentials(host, agent) == "copy"
    # Same mtime + size → fast-path "already fresh", no rewrite.
    assert link_host_credentials(host, agent) == "copy (fresh)"


def test_copy_fallback_refreshes_when_host_changes(tmp_path, monkeypatch):
    """When the host file changes (via claude refresh or claude login),
    a subsequent call must re-copy so the agent sees the new content.
    """
    import time as _time
    host = tmp_path / "host"
    agent = tmp_path / "agent"
    host_creds = _write_host_creds(host, '{"k":"old"}')
    monkeypatch.setattr(os, "symlink", lambda *a, **k: (_ for _ in ()).throw(OSError()))

    assert link_host_credentials(host, agent) == "copy"
    agent_creds = agent / ".claude" / ".credentials.json"
    assert agent_creds.read_text() == '{"k":"old"}'

    # Write a new host file with a clearly different mtime (use a
    # sleep gate rather than os.utime so we exercise the real
    # "host was just refreshed" code path).
    _time.sleep(0.05)
    host_creds.write_text('{"k":"new"}', encoding="utf-8")
    # Force mtime forward in case the FS has 1s granularity.
    future = host_creds.stat().st_mtime + 2
    os.utime(host_creds, (future, future))

    assert link_host_credentials(host, agent) == "copy"
    assert agent_creds.read_text() == '{"k":"new"}'


# ── Missing host file ────────────────────────────────────────────────────────


def test_missing_host_file_is_reportable_noop(tmp_path):
    host = tmp_path / "host"  # no .claude/
    agent = tmp_path / "agent"

    mode = link_host_credentials(host, agent)

    assert mode == "no-host-file"
    assert not (agent / ".claude" / ".credentials.json").exists()


def test_missing_host_file_doesnt_clobber_existing_agent_file(tmp_path):
    """If somehow the agent has creds but the host doesn't (odd
    scenario, but possible after a manual host cleanup), we should
    leave the agent file alone rather than ``unlink``-ing it."""
    host = tmp_path / "host"
    agent = tmp_path / "agent"
    agent_creds = agent / ".claude" / ".credentials.json"
    agent_creds.parent.mkdir(parents=True, exist_ok=True)
    agent_creds.write_text('{"k":"agent-has-it"}', encoding="utf-8")

    mode = link_host_credentials(host, agent)

    assert mode == "no-host-file"
    assert agent_creds.read_text() == '{"k":"agent-has-it"}'
