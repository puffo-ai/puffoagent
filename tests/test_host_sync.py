"""Tests for one-way sync of host-installed skills and MCP server
registrations into a cli-docker agent's per-agent virtual $HOME.

Contract:
  * Skills: each skill is a directory (``<name>/SKILL.md`` plus
    optional supporting files). We copy each host-side skill dir
    wholesale into ``<agent_home>/.claude/skills/``, drop a
    ``host-synced.md`` marker inside for provenance, prune stale
    host-synced dirs that the host no longer has, and never clobber
    a skill dir tagged ``agent-installed.md``.
  * MCPs: merge host ``~/.claude.json`` ``mcpServers`` into the
    per-agent ``.claude.json``; host wins on name collision;
    agent-only entries survive; other keys on ``.claude.json`` are
    left untouched (claude CLI manages them).
  * Unreachable detection: absolute paths under ``/Users/``,
    ``/home/<someone>/``, ``/tmp/``, ``/var/folders/``, or any
    Windows drive-letter/backslash path are flagged for a warning.
    Bare program names and ``/usr/bin``/``/opt`` paths pass
    through.
"""

from __future__ import annotations

import json

from puffoagent.portal.state import (
    AGENT_INSTALLED_MARKER,
    HOST_SYNCED_MARKER,
    _looks_host_local_command,
    sync_host_mcp_servers,
    sync_host_skills,
)


# ── Skills ───────────────────────────────────────────────────────────────────


def _write_skill(root, name, body="body", extra=None):
    """Create ``root/<name>/SKILL.md`` with optional supporting files."""
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(body, encoding="utf-8")
    for rel, content in (extra or {}).items():
        target = d / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return d


def test_sync_host_skills_copies_directory_form(tmp_path):
    host = tmp_path / "host"
    host_skills = host / ".claude" / "skills"
    _write_skill(host_skills, "one", body="A")
    _write_skill(
        host_skills, "two", body="B",
        extra={"reference.md": "ref", "scripts/helper.py": "print('x')"},
    )
    agent = tmp_path / "agent"

    n = sync_host_skills(host, agent)

    assert n == 2
    one = agent / ".claude" / "skills" / "one"
    two = agent / ".claude" / "skills" / "two"
    assert (one / "SKILL.md").read_text() == "A"
    assert (one / HOST_SYNCED_MARKER).exists()
    assert (two / "SKILL.md").read_text() == "B"
    assert (two / "reference.md").read_text() == "ref"
    assert (two / "scripts" / "helper.py").read_text() == "print('x')"
    assert (two / HOST_SYNCED_MARKER).exists()


def test_sync_host_skills_overwrites_existing_host_synced_dir(tmp_path):
    """When the host updates a skill, the agent picks up the new
    version on the next sync (and stale files in the old dir go away).
    """
    host = tmp_path / "host"
    _write_skill(host / ".claude" / "skills", "shared", body="v2")
    agent = tmp_path / "agent"
    # Simulate a previous sync that left stale_file + old SKILL.md.
    agent_dir = agent / ".claude" / "skills" / "shared"
    agent_dir.mkdir(parents=True)
    (agent_dir / "SKILL.md").write_text("v1", encoding="utf-8")
    (agent_dir / "stale_file.md").write_text("old", encoding="utf-8")
    (agent_dir / HOST_SYNCED_MARKER).write_text("", encoding="utf-8")

    sync_host_skills(host, agent)

    assert (agent_dir / "SKILL.md").read_text() == "v2"
    assert not (agent_dir / "stale_file.md").exists()
    assert (agent_dir / HOST_SYNCED_MARKER).exists()


def test_sync_host_skills_preserves_agent_installed_dirs(tmp_path):
    """A skill dir tagged ``agent-installed.md`` must survive the host
    sync untouched — even if the host has a skill with the same name.
    """
    host = tmp_path / "host"
    _write_skill(host / ".claude" / "skills", "collides", body="H")
    _write_skill(host / ".claude" / "skills", "from_host", body="HOST_ONLY")

    agent = tmp_path / "agent"
    agent_skills = agent / ".claude" / "skills"
    # Pre-existing agent-installed skills at user scope (edge case —
    # normally these live in workspace scope, but we guard here).
    agent_made = agent_skills / "collides"
    agent_made.mkdir(parents=True)
    (agent_made / "SKILL.md").write_text("AGENT", encoding="utf-8")
    (agent_made / AGENT_INSTALLED_MARKER).write_text("", encoding="utf-8")

    sync_host_skills(host, agent)

    assert (agent_made / "SKILL.md").read_text() == "AGENT"
    assert not (agent_made / HOST_SYNCED_MARKER).exists()
    assert (agent_skills / "from_host" / "SKILL.md").read_text() == "HOST_ONLY"


def test_sync_host_skills_prunes_removed_host_skills(tmp_path):
    """When the host removes a skill, a previously synced copy on the
    agent side is pruned — but only if we tagged it.
    """
    host = tmp_path / "host"
    (host / ".claude" / "skills").mkdir(parents=True)  # empty now
    agent = tmp_path / "agent"
    agent_skills = agent / ".claude" / "skills"
    # One dir tagged by us; one tagged by the agent; one untagged.
    for tag in (HOST_SYNCED_MARKER, AGENT_INSTALLED_MARKER, None):
        name = {
            HOST_SYNCED_MARKER: "was_host",
            AGENT_INSTALLED_MARKER: "agent_kept",
            None: "untagged",
        }[tag]
        d = agent_skills / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("x", encoding="utf-8")
        if tag:
            (d / tag).write_text("", encoding="utf-8")

    sync_host_skills(host, agent)

    assert not (agent_skills / "was_host").exists()
    assert (agent_skills / "agent_kept" / "SKILL.md").read_text() == "x"
    assert (agent_skills / "untagged" / "SKILL.md").read_text() == "x"


def test_sync_host_skills_ignores_flat_md_files(tmp_path):
    """Flat ``.md`` files at the top level of ``~/.claude/skills/`` are
    not valid Claude Code skills (the format is ``<name>/SKILL.md``),
    so sync skips them rather than propagating garbage.
    """
    host = tmp_path / "host"
    (host / ".claude" / "skills").mkdir(parents=True)
    (host / ".claude" / "skills" / "orphan.md").write_text("x", encoding="utf-8")
    _write_skill(host / ".claude" / "skills", "real_skill", body="Y")
    agent = tmp_path / "agent"

    n = sync_host_skills(host, agent)

    assert n == 1
    assert (agent / ".claude" / "skills" / "real_skill" / "SKILL.md").read_text() == "Y"
    assert not (agent / ".claude" / "skills" / "orphan.md").exists()


def test_sync_host_skills_missing_host_dir_is_noop(tmp_path):
    host = tmp_path / "host"  # no .claude/skills/
    agent = tmp_path / "agent"
    assert sync_host_skills(host, agent) == 0
    # No empty dst either — don't create it when there was nothing to copy.
    assert not (agent / ".claude" / "skills").exists()


# ── MCP registrations ────────────────────────────────────────────────────────


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_sync_host_mcp_merges_host_servers_into_empty_agent(tmp_path):
    host = tmp_path / "host"
    agent = tmp_path / "agent"
    _write_json(host / ".claude.json", {
        "mcpServers": {
            "fs": {"command": "npx", "args": ["-y", "server-fs"]},
        },
    })

    merged, unreachable = sync_host_mcp_servers(host, agent)

    assert merged == 1
    assert unreachable == []
    data = json.loads((agent / ".claude.json").read_text(encoding="utf-8"))
    assert data["mcpServers"]["fs"]["command"] == "npx"


def test_sync_host_mcp_preserves_agent_only_entries(tmp_path):
    host = tmp_path / "host"
    agent = tmp_path / "agent"
    _write_json(host / ".claude.json", {
        "mcpServers": {
            "fs": {"command": "npx", "args": []},
        },
    })
    _write_json(agent / ".claude.json", {
        "mcpServers": {
            "agent-only": {"command": "python3", "args": ["/workspace/a.py"]},
        },
        "somethingElse": {"keep": "me"},
    })

    merged, _ = sync_host_mcp_servers(host, agent)

    assert merged == 1
    data = json.loads((agent / ".claude.json").read_text(encoding="utf-8"))
    # both entries present
    assert set(data["mcpServers"].keys()) == {"fs", "agent-only"}
    # unrelated top-level keys preserved
    assert data["somethingElse"] == {"keep": "me"}


def test_sync_host_mcp_host_wins_on_collision(tmp_path):
    host = tmp_path / "host"
    agent = tmp_path / "agent"
    _write_json(host / ".claude.json", {
        "mcpServers": {
            "shared": {"command": "npx", "args": ["host-version"]},
        },
    })
    _write_json(agent / ".claude.json", {
        "mcpServers": {
            "shared": {"command": "npx", "args": ["agent-version"]},
        },
    })

    sync_host_mcp_servers(host, agent)

    data = json.loads((agent / ".claude.json").read_text(encoding="utf-8"))
    assert data["mcpServers"]["shared"]["args"] == ["host-version"]


def test_sync_host_mcp_flags_unreachable_command_paths(tmp_path):
    host = tmp_path / "host"
    agent = tmp_path / "agent"
    _write_json(host / ".claude.json", {
        "mcpServers": {
            "bare-ok": {"command": "npx", "args": []},
            "mac-local": {"command": "/Users/alice/bin/mcp", "args": []},
            "linux-home": {"command": "/home/bob/mcp", "args": []},
            "windows": {"command": r"C:\Users\bob\mcp.exe", "args": []},
            "container-ok": {"command": "/home/agent/.local/bin/mcp", "args": []},
            "sys-ok": {"command": "/usr/local/bin/node", "args": []},
        },
    })

    merged, unreachable = sync_host_mcp_servers(host, agent)

    assert merged == 6
    flagged_names = sorted(name for name, _ in unreachable)
    assert flagged_names == ["linux-home", "mac-local", "windows"]


def test_sync_host_mcp_no_host_file_is_noop(tmp_path):
    host = tmp_path / "host"  # no .claude.json
    agent = tmp_path / "agent"
    merged, unreachable = sync_host_mcp_servers(host, agent)
    assert merged == 0
    assert unreachable == []
    assert not (agent / ".claude.json").exists()


def test_sync_host_mcp_empty_host_servers_is_noop(tmp_path):
    host = tmp_path / "host"
    agent = tmp_path / "agent"
    _write_json(host / ".claude.json", {"mcpServers": {}})
    _write_json(agent / ".claude.json", {"mcpServers": {"keep": {"command": "npx"}}})

    merged, unreachable = sync_host_mcp_servers(host, agent)

    assert merged == 0
    assert unreachable == []
    # Agent file untouched — no rewrite performed.
    data = json.loads((agent / ".claude.json").read_text(encoding="utf-8"))
    assert data == {"mcpServers": {"keep": {"command": "npx"}}}


def test_sync_host_mcp_handles_empty_agent_file(tmp_path):
    """``docker_cli.py`` touches ``.claude.json`` to create a 0-byte
    file before ``docker run``. The merge must treat that as an
    empty config, not an error.
    """
    host = tmp_path / "host"
    agent = tmp_path / "agent"
    _write_json(host / ".claude.json", {
        "mcpServers": {"fs": {"command": "npx"}},
    })
    (agent / ".claude.json").parent.mkdir(parents=True, exist_ok=True)
    (agent / ".claude.json").touch()

    merged, _ = sync_host_mcp_servers(host, agent)

    assert merged == 1
    data = json.loads((agent / ".claude.json").read_text(encoding="utf-8"))
    assert "fs" in data["mcpServers"]


# ── Unreachable-command heuristic ────────────────────────────────────────────


def test_looks_host_local_command_passes_bare_names():
    for cmd in ("npx", "node", "python3", "uvx", "bash"):
        assert not _looks_host_local_command(cmd)


def test_looks_host_local_command_passes_container_paths():
    for cmd in (
        "/usr/bin/node",
        "/usr/local/bin/python3",
        "/opt/puffoagent-mcp/puffo_tools.py",
        "/home/agent/.local/bin/whatever",
        "/bin/sh",
    ):
        assert not _looks_host_local_command(cmd)


def test_looks_host_local_command_flags_host_paths():
    for cmd in (
        "/Users/alice/bin/mcp",
        "/home/bob/.local/bin/mcp",
        "/tmp/adhoc-server",
        "/var/folders/xy/T/mcp-12345",
        r"C:\Users\bob\mcp.exe",
        r"D:\apps\mcp.exe",
        "node C:\\stuff\\x.js",  # any backslash anywhere
    ):
        assert _looks_host_local_command(cmd), f"expected flagged: {cmd!r}"


def test_looks_host_local_command_empty_is_not_flagged():
    assert not _looks_host_local_command("")


# ── cli-local adapter integration ────────────────────────────────────────────
#
# cli-local calls sync_host_skills + sync_host_mcp_servers inside
# ``LocalCLIAdapter._verify()``. These tests construct an adapter
# pointed at a fake host/agent under tmp_path, monkeypatch HOME
# (what ``Path.home()`` reads), mock the ``claude`` binary check,
# then exercise ``_verify()``.


def _build_local_adapter(tmp_path, monkeypatch):
    """Shared harness: construct a LocalCLIAdapter with Path.home()
    redirected to ``tmp_path/host`` and the ``claude`` binary check
    mocked to succeed, returning the adapter plus the host/agent
    home paths for assertions.
    """
    host = tmp_path / "host"
    host.mkdir(parents=True, exist_ok=True)
    agent_home = tmp_path / "agent" / "home"
    # Path.home() reads HOME on POSIX, USERPROFILE on Windows. Set
    # both so the test runs on either platform.
    monkeypatch.setenv("HOME", str(host))
    monkeypatch.setenv("USERPROFILE", str(host))
    from puffoagent.agent.adapters import local_cli
    monkeypatch.setattr(local_cli.shutil, "which", lambda _: "/fake/claude")
    adapter = local_cli.LocalCLIAdapter(
        agent_id="t",
        model="",
        workspace_dir=str(tmp_path / "ws"),
        claude_dir=str(tmp_path / "ws" / ".claude"),
        session_file=str(tmp_path / "sess.json"),
        mcp_config_file=str(tmp_path / "mcp.json"),
        agent_home_dir=str(agent_home),
    )
    return adapter, host, agent_home


def test_local_cli_verify_syncs_host_skills(tmp_path, monkeypatch):
    adapter, host, agent_home = _build_local_adapter(tmp_path, monkeypatch)
    _write_skill(host / ".claude" / "skills", "s1", body="SKILL")

    adapter._verify()

    assert (agent_home / ".claude" / "skills" / "s1" / "SKILL.md").read_text() == "SKILL"
    assert (agent_home / ".claude" / "skills" / "s1" / HOST_SYNCED_MARKER).exists()


def test_local_cli_verify_merges_host_mcp_servers(tmp_path, monkeypatch):
    adapter, host, agent_home = _build_local_adapter(tmp_path, monkeypatch)
    _write_json(host / ".claude.json", {"mcpServers": {"fs": {"command": "npx"}}})

    adapter._verify()

    data = json.loads((agent_home / ".claude.json").read_text(encoding="utf-8"))
    assert data["mcpServers"]["fs"]["command"] == "npx"


def test_local_cli_verify_does_not_warn_on_host_local_mcp(
    tmp_path, monkeypatch, caplog,
):
    """On cli-local the agent subprocess runs on the host, so a host-
    local MCP command path (``/Users/…``, ``C:\\…``) WILL resolve.
    The unreachable-command warning is docker-only; cli-local must
    stay quiet for these so we don't cry wolf to the operator.
    """
    import logging
    adapter, host, _agent_home = _build_local_adapter(tmp_path, monkeypatch)
    _write_json(host / ".claude.json", {
        "mcpServers": {
            "mac-local": {"command": "/Users/alice/bin/mcp"},
            "win-local": {"command": r"C:\Users\bob\mcp.exe"},
        },
    })

    with caplog.at_level(logging.WARNING, logger="puffoagent.agent.adapters.local_cli"):
        adapter._verify()

    # No "host-local" warning. The dangerous-mode warning at the end
    # of _verify() is expected and filtered out.
    offending = [
        r for r in caplog.records
        if r.levelno >= logging.WARNING and "host-local" in r.message
    ]
    assert offending == []


def test_local_cli_verify_preserves_agent_installed_content(tmp_path, monkeypatch):
    """Skills/MCPs the agent registered for itself in a previous
    session must survive the host sync on the next worker start.
    """
    adapter, host, agent_home = _build_local_adapter(tmp_path, monkeypatch)
    # Host has its own skill + MCP
    _write_skill(host / ".claude" / "skills", "from_host", body="H")
    _write_json(host / ".claude.json", {
        "mcpServers": {"host-mcp": {"command": "npx"}},
    })
    # Agent already has an agent-installed skill + MCP from a previous
    # run. The skill carries the agent-installed marker so host sync
    # must leave it alone.
    agent_made = agent_home / ".claude" / "skills" / "agent_made"
    agent_made.mkdir(parents=True)
    (agent_made / "SKILL.md").write_text("A", encoding="utf-8")
    (agent_made / AGENT_INSTALLED_MARKER).write_text("", encoding="utf-8")
    _write_json(agent_home / ".claude.json", {
        "mcpServers": {"agent-mcp": {"command": "python3"}},
    })

    adapter._verify()

    # Both skills present, agent one untouched
    assert (agent_made / "SKILL.md").read_text() == "A"
    assert not (agent_made / HOST_SYNCED_MARKER).exists()
    assert (agent_home / ".claude" / "skills" / "from_host" / "SKILL.md").read_text() == "H"
    # Both MCPs present
    data = json.loads((agent_home / ".claude.json").read_text(encoding="utf-8"))
    assert set(data["mcpServers"].keys()) == {"agent-mcp", "host-mcp"}
