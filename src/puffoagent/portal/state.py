"""On-disk state layout for the multi-agent portal.

Everything the daemon and CLI coordinate on lives in the home directory
(default ``~/.puffoagent/``, overridable with ``PUFFOAGENT_HOME``):

::

    ~/.puffoagent/
      daemon.yml          # ai provider keys, defaults
      daemon.pid          # pid of the running daemon (managed by daemon)
      agents/
        <agent_id>/
          agent.yml       # mattermost url/token, channels, state
          profile.md      # system-prompt profile
          memory/         # per-agent memory + token_usage.json
      archived/
        <agent_id>-<ts>/

The CLI writes intent (create / pause / archive) into these files; the
daemon reconciler polls the tree and reconciles its in-memory task
registry with what's on disk. No IPC port, no auth — the filesystem is
the contract.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

import yaml


# Where daemon.yml, agents/, etc. live. Overridable for tests / alt users.
def home_dir() -> Path:
    override = os.environ.get("PUFFOAGENT_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".puffoagent"


def agents_dir() -> Path:
    return home_dir() / "agents"


def archived_dir() -> Path:
    return home_dir() / "archived"


def docker_dir() -> Path:
    """Root for puffoagent-owned docker-specific state (mcp scripts,
    shared primer, default-image build context). Not for per-agent
    claude state — see ``agent_home_dir`` for that."""
    return home_dir() / "docker"


def docker_shared_dir() -> Path:
    """Shared primer + skill markdown content all agents reference:
    folded into each agent's workspace/.claude/CLAUDE.md at worker
    startup."""
    return docker_dir() / "shared"


def agent_home_dir(agent_id: str) -> Path:
    """Per-agent "virtual $HOME". Used as ``HOME`` env for the
    cli-local claude subprocess and as the bind-mount source for
    cli-docker containers at ``/home/agent``.

    Claude Code reads its USER-level state from ``$HOME/.claude/``,
    so pointing HOME here gives each agent a fully isolated claude
    identity — own credentials cache, own session transcripts, own
    history.jsonl, no bleed between agents.
    """
    return agent_dir(agent_id)


def agent_claude_user_dir(agent_id: str) -> Path:
    """The ``.claude/`` inside the agent's virtual home — what Claude
    Code actually writes to. Seeded from the operator's real
    ``~/.claude/`` on first worker start (via ``seed_claude_home``)
    so a one-time ``claude login`` on the host carries over."""
    return agent_home_dir(agent_id) / ".claude"


def shared_fs_dir() -> Path:
    """Shared filesystem dir for cross-agent cooperation. Mounted
    into cli-docker containers at ``/workspace/.shared`` and
    referenced by absolute path for cli-local / sdk agents. Agents
    can coordinate via files dropped here."""
    return home_dir() / "shared"


# Files to copy from the operator's real ``$HOME`` into a per-agent
# virtual ``$HOME`` on first use. Paths are relative to ``$HOME``; we
# lift only OAuth-essential files, not multi-MB caches or transcripts
# the operator didn't produce for their bots.
#
# Note: ``.claude.json`` is a SIBLING of the ``.claude/`` dir, not
# inside it. Claude CLI reads it from ``$HOME/.claude.json`` so we
# mirror the same layout in the per-agent home.
_CLAUDE_HOME_SEED_PATHS = (
    ".claude/.credentials.json",
    ".claude/settings.json",
    ".claude.json",
)


def seed_claude_home(host_home: Path, agent_home: Path) -> bool:
    """Seed a per-agent virtual ``$HOME`` from the operator's real
    ``$HOME`` so each agent has its own isolated claude identity.
    Copies ``.claude/.credentials.json`` + ``.claude/settings.json``
    + sibling ``.claude.json``. Idempotent — never overwrites an
    existing file so refreshed tokens from prior bot runs survive.
    Returns True if any file was copied (diagnostic only).
    """
    import shutil
    agent_home.mkdir(parents=True, exist_ok=True)
    copied = False
    for rel in _CLAUDE_HOME_SEED_PATHS:
        src = host_home / rel
        dst = agent_home / rel
        if dst.exists() or not src.exists():
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied = True
        except OSError:
            continue
    return copied


def daemon_yml_path() -> Path:
    return home_dir() / "daemon.yml"


def daemon_pid_path() -> Path:
    return home_dir() / "daemon.pid"


def agent_dir(agent_id: str) -> Path:
    return agents_dir() / agent_id


def agent_yml_path(agent_id: str) -> Path:
    return agent_dir(agent_id) / "agent.yml"


def runtime_json_path(agent_id: str) -> Path:
    return agent_dir(agent_id) / "runtime.json"


def cli_session_json_path(agent_id: str) -> Path:
    """Persisted Claude Code session id for cli-local/cli-docker
    adapters. See ``agent/adapters/cli_session.py``.
    """
    return agent_dir(agent_id) / "cli_session.json"


# ─────────────────────────────────────────────────────────────────────────────
# Dataclasses
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class ProviderConfig:
    api_key: str = ""
    model: str = ""


@dataclass
class ServerConfig:
    """Puffo server connection for the daemon's control plane.

    When ``url`` + ``user_token`` are both set, the daemon runs in
    "server-synced" mode: it polls /api/v4/aiagents?owner=me every
    ``sync_interval_seconds`` and reconciles its local agents dir
    against the server's list of agents owned by this user.
    """
    url: str = ""
    user_token: str = ""
    sync_interval_seconds: float = 30.0


@dataclass
class DaemonConfig:
    """Contents of ~/.puffoagent/daemon.yml."""
    default_provider: str = "anthropic"
    anthropic: ProviderConfig = field(default_factory=ProviderConfig)
    openai: ProviderConfig = field(default_factory=ProviderConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    skills_dir: str = ""  # absolute path; empty means agents get no shared skills
    reconcile_interval_seconds: float = 2.0
    runtime_heartbeat_seconds: float = 5.0

    def has_server_sync(self) -> bool:
        return bool(self.server.url and self.server.user_token)

    @classmethod
    def load(cls) -> "DaemonConfig":
        path = daemon_yml_path()
        if not path.exists():
            return cls()
        with path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        cfg = cls(
            default_provider=raw.get("default_provider", "anthropic"),
            skills_dir=raw.get("skills_dir", ""),
            reconcile_interval_seconds=float(raw.get("reconcile_interval_seconds", 2.0)),
            runtime_heartbeat_seconds=float(raw.get("runtime_heartbeat_seconds", 5.0)),
        )
        for name in ("anthropic", "openai"):
            p = raw.get(name) or {}
            setattr(cfg, name, ProviderConfig(
                api_key=p.get("api_key", ""),
                model=p.get("model", ""),
            ))
        srv = raw.get("server") or {}
        cfg.server = ServerConfig(
            url=srv.get("url", ""),
            user_token=srv.get("user_token", ""),
            sync_interval_seconds=float(srv.get("sync_interval_seconds", 30.0)),
        )
        return cfg

    def save(self) -> None:
        path = daemon_yml_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "default_provider": self.default_provider,
            "skills_dir": self.skills_dir,
            "reconcile_interval_seconds": self.reconcile_interval_seconds,
            "runtime_heartbeat_seconds": self.runtime_heartbeat_seconds,
            "anthropic": asdict(self.anthropic),
            "openai": asdict(self.openai),
            "server": asdict(self.server),
        }
        _atomic_write_yaml(path, data)


@dataclass
class TriggerRules:
    on_mention: bool = True
    on_dm: bool = True


@dataclass
class MattermostConfig:
    url: str = ""
    bot_token: str = ""
    team_name: str = ""


@dataclass
class RuntimeConfig:
    """Contents of the ``runtime:`` block in agent.yml.

    ``kind`` selects which adapter handles this agent's turns. Empty
    strings for ``provider`` / ``model`` / ``api_key`` mean "inherit
    from daemon defaults". Kind-specific fields (docker image, tool
    allowlists, permission timeouts) are added here as new adapters
    land — see DESIGN.md.
    """
    kind: str = "chat-only"   # chat-only | sdk | cli-local | cli-docker
    provider: str = ""        # chat-only: anthropic | openai
    model: str = ""
    api_key: str = ""
    # Tool allowlist patterns (sdk | cli-local | cli-docker). Each entry
    # is either a bare tool name ("Read") or tool-name-plus-arg glob
    # ("Bash(git *)", "Read(**/*.py)"). Empty list = no tools allowed.
    allowed_tools: list[str] = field(default_factory=list)
    # cli-docker: override the default image tag. Empty → the bundled
    # image that puffoagent builds from its inline Dockerfile.
    docker_image: str = ""


@dataclass
class AgentConfig:
    """Contents of ~/.puffoagent/agents/<id>/agent.yml.

    The ``state`` field is the control knob the CLI flips to pause/resume
    an agent; the daemon picks up the change on the next reconcile tick.
    """
    id: str = ""
    state: str = "running"  # running | paused
    display_name: str = ""
    mattermost: MattermostConfig = field(default_factory=MattermostConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    profile: str = "profile.md"       # path relative to agent dir, or absolute
    memory_dir: str = "memory"        # path relative to agent dir, or absolute
    workspace_dir: str = "workspace"  # path relative to agent dir, or absolute
    # Per-agent .claude/ lives inside workspace_dir so the Claude Code
    # project-level convention (.claude/CLAUDE.md, .claude/skills/, etc)
    # is found automatically by cwd-based runtime discovery. Not a
    # user-configurable field on purpose — treat .claude/ as owned by
    # the adapter layer.
    triggers: TriggerRules = field(default_factory=TriggerRules)
    created_at: int = 0

    @classmethod
    def load(cls, agent_id: str) -> "AgentConfig":
        path = agent_yml_path(agent_id)
        with path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        mm = raw.get("mattermost") or {}
        rt = raw.get("runtime") or {}
        triggers = raw.get("triggers") or {}
        return cls(
            id=raw.get("id", agent_id),
            state=raw.get("state", "running"),
            display_name=raw.get("display_name", ""),
            mattermost=MattermostConfig(
                url=mm.get("url", ""),
                bot_token=mm.get("bot_token", ""),
                team_name=mm.get("team_name", ""),
            ),
            runtime=RuntimeConfig(
                kind=rt.get("kind", "chat-only"),
                provider=rt.get("provider", ""),
                model=rt.get("model", ""),
                api_key=rt.get("api_key", ""),
                allowed_tools=list(rt.get("allowed_tools") or []),
                docker_image=rt.get("docker_image", ""),
            ),
            profile=raw.get("profile", "profile.md"),
            memory_dir=raw.get("memory_dir", "memory"),
            workspace_dir=raw.get("workspace_dir", "workspace"),
            triggers=TriggerRules(
                on_mention=bool(triggers.get("on_mention", True)),
                on_dm=bool(triggers.get("on_dm", True)),
            ),
            created_at=int(raw.get("created_at", 0)),
        )

    def save(self) -> None:
        path = agent_yml_path(self.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "id": self.id,
            "state": self.state,
            "display_name": self.display_name,
            "created_at": self.created_at,
            "mattermost": asdict(self.mattermost),
            "runtime": asdict(self.runtime),
            "profile": self.profile,
            "memory_dir": self.memory_dir,
            "workspace_dir": self.workspace_dir,
            "triggers": asdict(self.triggers),
        }
        _atomic_write_yaml(path, data)

    def resolve_profile_path(self) -> Path:
        return self._resolve(self.profile)

    def resolve_memory_dir(self) -> Path:
        return self._resolve(self.memory_dir)

    def resolve_workspace_dir(self) -> Path:
        return self._resolve(self.workspace_dir)

    def resolve_claude_dir(self) -> Path:
        """Always ``<workspace>/.claude``. See the comment on
        ``AgentConfig`` for why this isn't user-configurable.
        """
        return self.resolve_workspace_dir() / ".claude"

    def _resolve(self, rel_or_abs: str) -> Path:
        p = Path(rel_or_abs)
        if p.is_absolute():
            return p
        return agent_dir(self.id) / p


@dataclass
class RuntimeState:
    """Written by the worker every ``runtime_heartbeat_seconds``.

    The CLI reads this for ``list`` / ``show``. ``updated_at`` is used to
    detect stale entries (daemon down or worker deadlocked).
    """
    status: str = "stopped"  # running | paused | error | stopped
    started_at: int = 0
    updated_at: int = 0
    msg_count: int = 0
    last_event_at: int = 0
    error: str = ""

    @classmethod
    def load(cls, agent_id: str) -> "RuntimeState | None":
        path = runtime_json_path(agent_id)
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as f:
                import json
                raw = json.load(f)
        except (OSError, ValueError):
            return None
        return cls(
            status=raw.get("status", "stopped"),
            started_at=int(raw.get("started_at", 0)),
            updated_at=int(raw.get("updated_at", 0)),
            msg_count=int(raw.get("msg_count", 0)),
            last_event_at=int(raw.get("last_event_at", 0)),
            error=raw.get("error", ""),
        )

    def save(self, agent_id: str) -> None:
        import json
        self.updated_at = int(time.time())
        path = runtime_json_path(agent_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(asdict(self), f, indent=2)
        os.replace(tmp, path)


# ─────────────────────────────────────────────────────────────────────────────
# Discovery + helpers
# ─────────────────────────────────────────────────────────────────────────────


_AGENT_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")


def is_valid_agent_id(agent_id: str) -> bool:
    return bool(_AGENT_ID_RE.match(agent_id)) and len(agent_id) <= 64


def discover_agents() -> list[str]:
    """Return agent ids in lexicographic order. Does not load their config."""
    root = agents_dir()
    if not root.exists():
        return []
    return sorted(
        entry.name for entry in root.iterdir()
        if entry.is_dir() and (entry / "agent.yml").exists()
    )


def read_daemon_pid() -> int | None:
    path = daemon_pid_path()
    if not path.exists():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def is_daemon_alive() -> bool:
    pid = read_daemon_pid()
    if pid is None:
        return False
    if os.name == "nt":
        # On Windows, os.kill(pid, 0) is not a reliable presence check —
        # it raises WinError 87 even for live processes. Use
        # OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION) instead.
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            return True
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False


def write_daemon_pid(pid: int) -> None:
    path = daemon_pid_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(pid), encoding="utf-8")


def clear_daemon_pid() -> None:
    path = daemon_pid_path()
    if path.exists():
        try:
            path.unlink()
        except OSError:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Atomic YAML write
# ─────────────────────────────────────────────────────────────────────────────


def _atomic_write_yaml(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        # ``allow_unicode=True`` keeps non-ASCII characters as real
        # UTF-8 in the file rather than escaping to ``\uXXXX`` — much
        # easier to eyeball agent.yml when display_name is CJK /
        # emoji / accented etc.
        yaml.safe_dump(
            data, f,
            sort_keys=False,
            default_flow_style=False,
            allow_unicode=True,
        )
    os.replace(tmp, path)
