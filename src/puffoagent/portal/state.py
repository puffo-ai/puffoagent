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

import json
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
# Files the per-agent virtual ``$HOME`` gets on first use.
# ``.credentials.json`` is deliberately NOT in this list — it's
# set up separately via ``link_host_credentials`` below so every
# agent tracks the operator's live OAuth state (matches cli-docker's
# single-file bind-mount model).
_CLAUDE_HOME_SEED_PATHS = (
    ".claude/settings.json",
    ".claude.json",
)


def seed_claude_home(host_home: Path, agent_home: Path) -> bool:
    """Seed a per-agent virtual ``$HOME`` from the operator's real
    ``$HOME`` so each agent has its own isolated claude identity.
    Copies ``.claude/settings.json`` + sibling ``.claude.json``.
    Idempotent — never overwrites an existing file so agent-side
    customisation survives across restarts.

    ``.credentials.json`` is NOT seeded here — see
    ``link_host_credentials``. It's symlinked (or copied) from the
    host's live file so every agent tracks operator re-logins
    automatically, the way cli-docker's bind-mount already does.

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


def link_host_credentials(host_home: Path, agent_home: Path) -> str:
    """Point the agent's ``.credentials.json`` at the operator's
    live host file so OAuth state is SHARED across cli-local agents
    (and the operator's own ``claude`` usage), matching cli-docker's
    single-file bind-mount semantics.

    Why share: Anthropic OAuth uses rotating refresh tokens. The
    original per-agent-copy design caused agents' copies to go stale
    whenever the operator re-ran ``claude login`` — the host's copy
    refreshed, but the agent's stayed frozen until the next worker
    restart, and eventually its refresh token itself expired with no
    recovery path. Sharing one file flips the model: any refresh
    (host, any agent) updates the single file; every other consumer
    sees it on the next read.

    Preference order:

    1. **Symlink** — best: read-through is free, atomic-rename writes
       on the host side automatically re-resolve through the symlink,
       no periodic sync needed.
    2. **Copy** — fallback for Windows-without-Developer-Mode or other
       locked-down systems where ``os.symlink`` fails. Re-copies if
       host mtime > agent mtime; callers should invoke this helper
       periodically (on worker start AND on every refresh_ping tick)
       so copy-mode agents stay close to live.

    Hardlinks are deliberately skipped — claude rewrites the
    credentials file with an atomic tmp+rename, which breaks the
    shared inode. Symlinks survive that pattern; copies just get
    re-copied on the next tick.

    Idempotent. Returns ``"symlink"``, ``"symlink (already)"``,
    ``"copy"``, ``"copy (fresh)"``, or ``"no-host-file"`` as a
    diagnostic.
    """
    import shutil
    host_creds = host_home / ".claude" / ".credentials.json"
    agent_creds = agent_home / ".claude" / ".credentials.json"
    if not host_creds.exists():
        return "no-host-file"
    agent_creds.parent.mkdir(parents=True, exist_ok=True)

    # Fast path: agent already has a symlink pointing at host_creds.
    # Nothing to do — readers see live host state through it.
    if agent_creds.is_symlink():
        try:
            current = os.readlink(agent_creds)
            if Path(current) == host_creds or current == str(host_creds):
                return "symlink (already)"
        except OSError:
            pass

    # Fast path: agent has a regular copy that's already up-to-date.
    # Checked before we tear down the file — no pointless rewrites.
    if (
        agent_creds.exists()
        and not agent_creds.is_symlink()
        and _file_is_up_to_date(agent_creds, host_creds)
    ):
        return "copy (fresh)"

    # Tear down whatever's there so we can create a fresh symlink /
    # copy. Swallow unlink errors — on Windows a race against another
    # process holding the file open can fail; the subsequent create
    # path will retry naturally on the next call.
    try:
        if agent_creds.is_symlink() or agent_creds.exists():
            agent_creds.unlink()
    except OSError:
        pass

    try:
        os.symlink(host_creds, agent_creds)
        return "symlink"
    except (OSError, NotImplementedError):
        pass

    try:
        shutil.copy2(host_creds, agent_creds)
        return "copy"
    except OSError:
        return "no-host-file"


def _file_is_up_to_date(dst: Path, src: Path) -> bool:
    """True when ``dst`` and ``src`` have the same mtime + size.
    Used by ``link_host_credentials`` to skip unnecessary re-copies
    in the Windows-without-Developer-Mode fallback path."""
    try:
        ds, ss = dst.stat(), src.stat()
    except OSError:
        return False
    return ds.st_mtime == ss.st_mtime and ds.st_size == ss.st_size


# Marker files dropped inside every skill directory to tag who
# owns it. Claude Code only loads SKILL.md as a skill's entrypoint;
# these siblings are inert unless referenced from SKILL.md, so
# they're safe to drop for provenance tracking.
HOST_SYNCED_MARKER = "host-synced.md"
AGENT_INSTALLED_MARKER = "agent-installed.md"

_HOST_SYNCED_MARKER_BODY = (
    "This skill is synced from the operator's ~/.claude/skills/ on "
    "every worker start. Do not edit; changes will be overwritten.\n"
)
_AGENT_INSTALLED_MARKER_BODY = (
    "This skill was installed by the agent via the install_skill "
    "MCP tool. It lives at project scope and survives host syncs.\n"
)


def sync_host_skills(host_home: Path, agent_home: Path) -> int:
    """Copy every skill *directory* from the operator's real
    ``~/.claude/skills/`` into the per-agent virtual ``$HOME``'s
    user-level skills dir. Each skill is a subdirectory containing
    at least ``SKILL.md`` (the Claude Code skill format), so we copy
    whole trees rather than flat ``*.md`` files.

    Semantics:
      * Every synced dir gets a ``host-synced.md`` marker so provenance
        is discoverable at list time and the pruner below knows what
        it owns.
      * On name collision with an agent-installed skill (marker present),
        the agent's copy is preserved — host never clobbers agent work,
        even if the same name lands in user scope by mistake.
      * Stale host-synced skills (host removed the skill) are pruned
        from the agent dir so old copies don't shadow current ones.
      * Flat ``.md`` files at the top level of ``~/.claude/skills/``
        are ignored — they aren't valid Claude Code skills.

    Returns the number of skill directories copied (diagnostic only).
    """
    import shutil
    src = host_home / ".claude" / "skills"
    dst_root = agent_home / ".claude" / "skills"

    host_names: set[str] = set()
    if src.is_dir():
        host_names = {p.name for p in src.iterdir() if p.is_dir()}

    copied = 0
    if host_names:
        dst_root.mkdir(parents=True, exist_ok=True)
        for name in sorted(host_names):
            src_dir = src / name
            dst_dir = dst_root / name
            if (dst_dir / AGENT_INSTALLED_MARKER).exists():
                continue
            try:
                if dst_dir.exists():
                    shutil.rmtree(dst_dir)
                shutil.copytree(src_dir, dst_dir)
                (dst_dir / HOST_SYNCED_MARKER).write_text(
                    _HOST_SYNCED_MARKER_BODY, encoding="utf-8",
                )
                copied += 1
            except OSError:
                continue

    if dst_root.is_dir():
        for entry in dst_root.iterdir():
            if not entry.is_dir() or entry.name in host_names:
                continue
            if (entry / HOST_SYNCED_MARKER).exists() and not (
                entry / AGENT_INSTALLED_MARKER
            ).exists():
                try:
                    shutil.rmtree(entry)
                except OSError:
                    pass

    return copied


# Commands that reference these prefixes on the host won't resolve
# inside the container's Linux filesystem. ``/home/agent/`` IS valid
# inside the container and is handled separately below.
_HOST_LOCAL_COMMAND_PREFIXES = ("/Users/", "/tmp/", "/var/folders/")


def _looks_host_local_command(command: str) -> bool:
    """Return True when ``command`` points at a host-specific path
    that won't exist inside a puffoagent-runtime container. Used to
    surface a warning when merging host MCP registrations into a
    per-agent ``.claude.json``. Conservative by design — only flags
    paths we're confident don't resolve. Bare program names
    (``npx``, ``python3``) pass through without a warning because
    they're expected on PATH in the image.
    """
    if not command:
        return False
    # Windows drive-letter paths or backslash separators can't
    # possibly resolve inside a Linux container.
    if re.match(r"^[A-Za-z]:[\\/]", command) or "\\" in command:
        return True
    # Any /home/ path other than the container's own /home/agent/ is
    # an operator's home dir on the host and won't exist inside.
    if command.startswith("/home/") and not command.startswith("/home/agent/"):
        return True
    return any(command.startswith(p) for p in _HOST_LOCAL_COMMAND_PREFIXES)


def sync_host_mcp_servers(
    host_home: Path, agent_home: Path,
) -> tuple[int, list[tuple[str, str]]]:
    """Merge the operator's user-level MCP registrations (``~/.claude.
    json``'s ``mcpServers`` key) into the per-agent ``.claude.json``.

    Semantics:
      * host-registered names overwrite the per-agent entry on name
        collision (host is source of truth for host-installed MCPs);
      * names that only exist in the per-agent file are preserved
        (those were registered by the agent itself and must survive);
      * every other key on ``.claude.json`` is left untouched so the
        claude CLI can keep managing its own metadata there.

    Returns ``(merged_count, unreachable)`` — ``merged_count`` is the
    number of host entries written into the per-agent file;
    ``unreachable`` lists ``(name, command)`` pairs whose ``command``
    looks host-local (absolute path that won't resolve inside the
    container). The caller is expected to log a warning for each.
    """
    host_path = host_home / ".claude.json"
    if not host_path.exists():
        return 0, []
    try:
        host_data = json.loads(host_path.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return 0, []
    host_servers = host_data.get("mcpServers") or {}
    if not host_servers:
        return 0, []

    agent_path = agent_home / ".claude.json"
    agent_data: dict[str, Any] = {}
    if agent_path.exists():
        try:
            raw = agent_path.read_text(encoding="utf-8")
            if raw.strip():
                agent_data = json.loads(raw)
        except (OSError, ValueError):
            agent_data = {}

    agent_servers = dict(agent_data.get("mcpServers") or {})
    unreachable: list[tuple[str, str]] = []
    for name, cfg in host_servers.items():
        agent_servers[name] = cfg
        if isinstance(cfg, dict):
            cmd = cfg.get("command") or ""
            if isinstance(cmd, str) and _looks_host_local_command(cmd):
                unreachable.append((name, cmd))
    agent_data["mcpServers"] = agent_servers

    try:
        agent_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = agent_path.with_suffix(agent_path.suffix + ".tmp")
        tmp.write_text(json.dumps(agent_data, indent=2), encoding="utf-8")
        os.replace(tmp, agent_path)
    except OSError:
        return 0, []
    return len(host_servers), unreachable


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
    # Server-issued device id, set when the user paired this machine
    # via `puffoagent login` (no args). Empty for legacy installs that
    # used --url + --token directly. The server's sync filter uses
    # this to scope agents to the device hosting them.
    device_id: str = ""
    # Username of the daemon operator (the human who ran `puffoagent
    # login`). Captured from GET /users/me after auth. Used as the DM
    # target for the cli-local permission proxy — by design, tool-
    # approval prompts go to the operator of this daemon, regardless
    # of which user actually created the agent.
    operator_username: str = ""


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
            device_id=srv.get("device_id", ""),
            operator_username=srv.get("operator_username", ""),
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
    # cli-local: Claude Code permission mode. See
    # https://code.claude.com/docs/en/permission-modes for what each
    # value auto-approves. ``default`` (claude's built-in) auto-
    # approves reads and routes everything else through our
    # permission-prompt-tool proxy — most agents should use this.
    permission_mode: str = "default"


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
                permission_mode=rt.get("permission_mode", "default"),
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
    # Claude-side health, independent of the worker lifecycle
    # ``status``. "ok" = last smoke test passed; "auth_failed" =
    # refresh-ping detected a 401 / authentication_error; "unknown"
    # = no probe has run yet (worker just started). Written by the
    # credential_refresh task after each tick based on the adapter's
    # auth_healthy flag. See 2026-04-21 Core 3 freeze incident.
    health: str = "unknown"  # ok | auth_failed | unknown

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
            health=raw.get("health", "unknown"),
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
