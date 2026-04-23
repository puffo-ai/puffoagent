"""Self-contained MCP server exposing Puffo.ai tools to AI agents.

Runs as a stdio subprocess spawned by the claude CLI (or
claude-agent-sdk), registered via ``--mcp-config`` or
``ClaudeAgentOptions.mcp_servers``. The server needs no puffoagent
import surface at runtime — only ``aiohttp`` and ``mcp`` — so we can
run the same file on the host (cli-local), inside the cli-docker
container (bind-mounted), or inside the SDK adapter's process.

Tools exposed (prefixed ``mcp__puffo__`` when invoked from claude):

    whoami()
        Return your own bot identity: username (the @-handle others
        mention you with), user_id, first/last name, nickname, and
        the AIAgent display_name + team. Useful when you need to
        introduce yourself or recognise mentions.

    send_message(channel, text, root_id="")
        Post to a channel or DM by name/id. Returns the new post id.

    upload_file(path, channel, caption="")
        Upload a workspace-relative file to a channel + post it.

    list_channels(team="")
        List channels the bot is a member of. Lightweight discovery.

    list_channel_members(channel)
        Return the usernames + types of every member of a channel.

    get_channel_history(channel, limit=20)
        Fetch the last N posts in a channel. Each post lists its
        sender (with bot/human type), timestamp, text, and attached
        file names.

    fetch_channel_files(channel, limit=20)
        Back-fill attachments from recent channel history into the
        agent's workspace at ``attachments/<post_id>/<filename>``.
        Useful when joining a channel that already has file history.

    get_post(post_ref)
        Fetch one post by id or permalink URL. Returns text + sender
        + timestamp + attachment list.

    get_user_info(username)
        Look up a user by @-handle. Returns username, display name,
        email, and bot/human type.

    reload_system_prompt()
        Ask the daemon to rebuild your CLAUDE.md from disk and
        restart your claude subprocess so fresh edits to your
        profile/memory/CLAUDE.md take effect on your next message.

    install_skill(name, content) / uninstall_skill(name) / list_skills()
        Manage project-scope skills in ``<workspace>/.claude/skills/``.
        install drops a ``SKILL.md`` + ``agent-installed.md`` marker;
        uninstall refuses to touch host-synced skills; list tags each
        entry ``[system]`` or ``[agent]``. Call ``refresh()`` after
        installs to pick them up.

    install_mcp_server(name, command, args, env)
    uninstall_mcp_server(name) / list_mcp_servers()
        Manage project-scope MCP servers in ``<workspace>/.mcp.json``.
        Host-local command paths are rejected. System MCPs (from the
        operator's ``~/.claude.json``) can't be removed here. Call
        ``refresh()`` after installs.

    refresh(model=None)
        Respawn your claude subprocess (via ``--resume``, history
        preserved) so new skills/MCPs are discovered. Optional
        ``model`` argument switches runtime model. Lighter than
        ``reload_system_prompt`` — does NOT regenerate CLAUDE.md.

    approve_permission(tool_name, input)
        (cli-local permission proxy.) Post a permission request to the
        owner's DM and poll for a reply ('y'/'n'/'approve'/'deny') up
        to ``--permission-timeout`` seconds, then allow/deny.

Run standalone:

    python -m puffoagent.mcp.puffo_tools \\
        --agent-id han-docker \\
        --url https://app.puffo.ai \\
        --token <bot-token> \\
        --workspace /path/to/workspace \\
        [--team puffo-core] \\
        [--owner-username han.dev] \\
        [--permission-timeout 300]

For embedding in-process (SDK adapter), call ``build_server(cfg)``.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import aiohttp
from mcp.server.fastmcp import FastMCP


# Mattermost permalink: https://<host>/<team>/pl/<26-char post id>
_PERMALINK_RE = re.compile(r"/pl/([a-z0-9]{26})(?:[/?#].*)?$")
# Bare post id: exactly 26 a-z0-9 chars.
_POST_ID_RE = re.compile(r"^[a-z0-9]{26}$")

# Skill slug: lowercase letters, digits, hyphens; can't lead with a
# hyphen; max 64 chars. Matches Claude Code's documented constraint.
_SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

# Provenance markers dropped inside every skill directory. Claude
# Code only executes ``SKILL.md`` as a skill entrypoint, so siblings
# are inert unless referenced from SKILL.md — safe to use as tags.
AGENT_INSTALLED_MARKER = "agent-installed.md"
HOST_SYNCED_MARKER = "host-synced.md"
_AGENT_INSTALLED_BODY = (
    "This skill was installed by the agent via the install_skill "
    "MCP tool. It lives at project scope and survives host syncs.\n"
)

# Command-path prefixes that won't resolve inside a puffoagent
# runtime container. Duplicated from ``puffoagent.portal.state`` so
# this module stays importable with only ``aiohttp`` + ``mcp`` — it
# runs standalone on the host (cli-local) and bind-mounted inside
# the cli-docker container, neither of which has puffoagent itself
# on the Python path.
_HOST_LOCAL_PREFIXES = ("/Users/", "/tmp/", "/var/folders/")


def _looks_host_local_command(command: str) -> bool:
    """Return True when ``command`` points at a host-specific path
    that won't exist inside a puffoagent-runtime container. Used to
    reject MCP server registrations whose command would fail to
    spawn. Bare program names (``npx``, ``uvx``, ``python3``) pass
    through because they're expected to resolve on PATH.
    """
    if not command:
        return False
    if re.match(r"^[A-Za-z]:[\\/]", command) or "\\" in command:
        return True
    if command.startswith("/home/") and not command.startswith("/home/agent/"):
        return True
    return any(command.startswith(p) for p in _HOST_LOCAL_PREFIXES)


# ── Skill / MCP install helpers ─────────────────────────────────────────────
#
# Pure-Path implementations of the install / uninstall / list /
# refresh MCP tools. Kept at module level (not inside build_server)
# so tests can drive them directly without standing up a FastMCP
# server. The @mcp.tool() wrappers below are thin shims that turn
# results into human-readable strings.


def _workspace_skills_dir(workspace: Path) -> Path:
    """Project-scope skills dir for an agent — what install_skill
    writes. Claude Code also reads this alongside user-scope skills
    at session start."""
    return workspace / ".claude" / "skills"


def _system_skills_dir(home: Path) -> Path:
    """User-scope skills dir — operator-managed, host-synced."""
    return home / ".claude" / "skills"


def _workspace_mcp_path(workspace: Path) -> Path:
    """Project-scope MCP config. Claude Code's documented project-
    scope filename is ``.mcp.json`` at the workspace root —
    https://code.claude.com/docs/en/mcp."""
    return workspace / ".mcp.json"


def _system_claude_json_path(home: Path) -> Path:
    """User-scope claude config — contains system-scope MCPs."""
    return home / ".claude.json"


def _read_json_or_empty(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise RuntimeError(
            f"existing config at {path} is malformed JSON: {exc}. "
            "fix or delete the file before retrying."
        ) from exc
    return data if isinstance(data, dict) else {}


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _install_skill(workspace: Path, name: str, content: str) -> Path:
    if not _SKILL_NAME_RE.match(name or ""):
        raise RuntimeError(
            f"invalid skill name {name!r}: must be lowercase letters, "
            "digits, and hyphens (max 64 chars, can't start with a hyphen)"
        )
    if not content or not content.strip():
        raise RuntimeError("skill content is empty")
    dst = _workspace_skills_dir(workspace) / name
    dst.mkdir(parents=True, exist_ok=True)
    (dst / "SKILL.md").write_text(content, encoding="utf-8")
    (dst / AGENT_INSTALLED_MARKER).write_text(
        _AGENT_INSTALLED_BODY, encoding="utf-8",
    )
    return dst


def _uninstall_skill(workspace: Path, name: str) -> Path:
    if not _SKILL_NAME_RE.match(name or ""):
        raise RuntimeError(f"invalid skill name {name!r}")
    dst = _workspace_skills_dir(workspace) / name
    if not dst.is_dir():
        raise RuntimeError(
            f"no agent-installed skill {name!r} at {dst}. "
            "use list_skills() to see what's available."
        )
    if not (dst / AGENT_INSTALLED_MARKER).exists():
        raise RuntimeError(
            f"skill {name!r} at {dst} has no {AGENT_INSTALLED_MARKER} "
            "marker — refusing to delete to avoid clobbering "
            "operator-managed content."
        )
    shutil.rmtree(dst)
    return dst


def _list_skills(workspace: Path, home: Path) -> list[tuple[str, str]]:
    """Return ``[(scope, name), ...]`` sorted by scope then name,
    where scope is either ``"system"`` or ``"agent"``.
    """
    out: list[tuple[str, str]] = []
    sysroot = _system_skills_dir(home)
    if sysroot.is_dir():
        for d in sorted(sysroot.iterdir()):
            if d.is_dir() and (d / "SKILL.md").exists():
                out.append(("system", d.name))
    agentroot = _workspace_skills_dir(workspace)
    if agentroot.is_dir():
        for d in sorted(agentroot.iterdir()):
            if d.is_dir() and (d / "SKILL.md").exists():
                out.append(("agent", d.name))
    return out


def _install_mcp_server(
    workspace: Path,
    name: str,
    command: str,
    args: Optional[list[str]] = None,
    env: Optional[dict[str, str]] = None,
    check_host_local: bool = True,
) -> Path:
    """Install a project-scope MCP server.

    ``check_host_local`` is ``True`` for runtimes where the agent
    process sees a different filesystem than the operator (cli-docker,
    sdk-in-container) — host paths like ``/Users/alice/bin/mcp`` or
    ``/home/operator/...`` won't resolve and we reject them at
    install time. ``False`` for cli-local where the agent runs on
    the host, so host paths DO resolve and the check would produce
    false positives.
    """
    if not name or not isinstance(name, str) or len(name) > 64:
        raise RuntimeError(
            f"invalid MCP server name {name!r}: required, string, max 64 chars"
        )
    if not command or not isinstance(command, str):
        raise RuntimeError("command is required")
    if check_host_local and _looks_host_local_command(command):
        raise RuntimeError(
            f"refusing to register command {command!r}: looks host-local "
            "(absolute path that won't resolve inside the runtime). Use "
            "a bare program name (npx, uvx, python3) or an absolute "
            "path that exists in the container."
        )
    path = _workspace_mcp_path(workspace)
    data = _read_json_or_empty(path)
    servers = dict(data.get("mcpServers") or {})
    servers[name] = {
        "command": command,
        "args": list(args or []),
        "env": dict(env or {}),
    }
    data["mcpServers"] = servers
    _atomic_write_json(path, data)
    return path


def _uninstall_mcp_server(workspace: Path, name: str) -> Path:
    if not name or not isinstance(name, str):
        raise RuntimeError("name is required")
    path = _workspace_mcp_path(workspace)
    if not path.exists():
        raise RuntimeError(
            f"no project-scope MCP config at {path}. nothing to remove."
        )
    data = _read_json_or_empty(path)
    servers = dict(data.get("mcpServers") or {})
    if name not in servers:
        raise RuntimeError(
            f"no agent-installed MCP server {name!r} at project scope. "
            "use list_mcp_servers() to see what's available. system MCPs "
            "can't be removed from here."
        )
    servers.pop(name)
    data["mcpServers"] = servers
    _atomic_write_json(path, data)
    return path


def _list_mcp_servers(workspace: Path, home: Path) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    try:
        sys_data = _read_json_or_empty(_system_claude_json_path(home))
    except RuntimeError:
        sys_data = {}
    for n in sorted((sys_data.get("mcpServers") or {}).keys()):
        out.append(("system", n))
    try:
        agent_data = _read_json_or_empty(_workspace_mcp_path(workspace))
    except RuntimeError:
        agent_data = {}
    for n in sorted((agent_data.get("mcpServers") or {}).keys()):
        out.append(("agent", n))
    return out


def _write_refresh_flag(workspace: Path, model: Optional[str]) -> Path:
    """Drop the refresh-flag file the worker watches on next turn.
    Returns the flag path. ``model`` is either None (no override),
    a non-empty string (switch to this model), or ``""`` (clear back
    to the daemon default)."""
    payload: dict[str, Any] = {"requested_at": int(time.time())}
    if model is not None:
        if not isinstance(model, str):
            raise RuntimeError("model must be a string (or omitted)")
        payload["model"] = model.strip()
    flag_path = workspace / ".puffoagent" / "refresh.flag"
    try:
        flag_path.parent.mkdir(parents=True, exist_ok=True)
        flag_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"could not write refresh flag: {exc}") from exc
    return flag_path


def _parse_post_ref(ref: str) -> str:
    """Accept either a raw 26-char post id or a Mattermost permalink
    URL and return the post id. Raises on anything else.
    """
    ref = (ref or "").strip()
    if _POST_ID_RE.match(ref):
        return ref
    m = _PERMALINK_RE.search(ref)
    if m:
        return m.group(1)
    raise RuntimeError(
        f"cannot parse post ref {ref!r}: expected a 26-char post id "
        "or a /pl/<id> permalink"
    )


def _ts_to_iso(ms: int) -> str:
    """Mattermost timestamps are milliseconds since epoch. Render as
    UTC ISO for display."""
    if not ms:
        return ""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat(timespec="seconds")

logger = logging.getLogger("puffoagent.mcp.puffo_tools")


@dataclass
class ToolsConfig:
    """Everything the tools need at runtime. Populated from argv/env
    at startup and captured in closures — never mutated afterwards.
    """
    agent_id: str
    url: str
    token: str
    workspace: str
    team: str = ""
    owner_username: str = ""
    permission_timeout_seconds: float = 300.0
    # Which adapter spawned this MCP server. Lets tools make
    # runtime-aware decisions — e.g., install_mcp_server accepts
    # host-local command paths on cli-local (agent runs on host)
    # but rejects them on cli-docker (container can't see them).
    # Set via ``PUFFO_RUNTIME_KIND`` env var. Values:
    # ``cli-local``, ``cli-docker``, ``sdk``, or empty (unknown).
    runtime_kind: str = ""
    # Which agent engine is running (``claude-code`` / ``hermes`` /
    # empty). Claude-Code-specific tools — install_skill, refresh,
    # the project-scope .mcp.json writer — short-circuit with a
    # clear error when this isn't ``claude-code`` because their
    # side effects only mean anything inside the Claude Code runtime.
    harness: str = ""


# ── HTTP helpers ──────────────────────────────────────────────────────────────


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _format_http_error(method: str, path: str, status: int, body: str) -> str:
    """Translate a non-2xx Mattermost response into an actionable
    message the LLM can read in a tool result.

    403 and 404 on a channel / post / team endpoint usually mean the
    bot lost membership (removed from the channel or team) or the
    target was deleted. Both are *terminal* from the tool's
    perspective — retrying the same call will keep failing until an
    operator fixes the membership. The phrasing tells the LLM to
    stop retrying rather than loop back into the same call.

    All other statuses just surface the raw body; those are usually
    transient (5xx server error, rate limits, etc.) where retrying
    later makes sense.
    """
    snippet = (body or "")[:200]
    if status == 403:
        return (
            f"{method} {path} -> 403 Forbidden: {snippet} "
            "— your bot account likely lost membership in this channel "
            "or team (an operator removed you). Do NOT retry the same "
            "call; either address a different channel, ask the user to "
            "re-invite the bot, or stop."
        )
    if status == 404:
        return (
            f"{method} {path} -> 404 Not Found: {snippet} "
            "— the channel / post / user referenced was deleted or "
            "never existed. Do NOT retry the same call; pick a "
            "different target or stop."
        )
    return f"{method} {path} -> {status}: {snippet}"


async def _get(session: aiohttp.ClientSession, url: str, path: str) -> Any:
    async with session.get(url.rstrip("/") + path) as resp:
        if resp.status != 200:
            body = await resp.text()
            raise RuntimeError(_format_http_error("GET", path, resp.status, body))
        return await resp.json()


async def _post(
    session: aiohttp.ClientSession, url: str, path: str, payload: Any,
) -> Any:
    async with session.post(url.rstrip("/") + path, json=payload) as resp:
        if resp.status not in (200, 201):
            body = await resp.text()
            raise RuntimeError(_format_http_error("POST", path, resp.status, body))
        return await resp.json()


# ── Channel resolution ────────────────────────────────────────────────────────


async def _resolve_channel(
    session: aiohttp.ClientSession,
    cfg: ToolsConfig,
    bot_user_id: str,
    ref: str,
) -> str:
    """Map a user-supplied channel reference to a Mattermost channel id.

    Supported forms:
      - ``@username``   DM with that user (created if missing).
      - ``#name``       public/private channel by name in ``cfg.team``.
      - ``<id>``        bare 26-char channel id, returned as-is.
    """
    ref = ref.strip()
    if not ref:
        raise RuntimeError("channel ref is empty")

    if ref.startswith("@"):
        username = ref[1:]
        user = await _get(session, cfg.url, f"/api/v4/users/username/{username}")
        other_id = user["id"]
        channel = await _post(
            session, cfg.url, "/api/v4/channels/direct",
            [bot_user_id, other_id],
        )
        return channel["id"]

    if ref.startswith("#"):
        name = ref[1:]
        if not cfg.team:
            raise RuntimeError(
                f"channel '{ref}' needs a team. start the MCP server with --team"
            )
        team = await _get(session, cfg.url, f"/api/v4/teams/name/{cfg.team}")
        channel = await _get(
            session, cfg.url,
            f"/api/v4/teams/{team['id']}/channels/name/{name}",
        )
        return channel["id"]

    # Treat as a raw channel id.
    return ref


async def _get_me(session: aiohttp.ClientSession, cfg: ToolsConfig) -> dict:
    return await _get(session, cfg.url, "/api/v4/users/me")


async def _resolve_root_id(
    session: aiohttp.ClientSession,
    cfg: ToolsConfig,
    root_id: str,
    target_channel_id: str,
) -> tuple[str, str]:
    """Validate + normalise a caller-supplied root_id before POSTing.

    Mattermost rejects ``root_id`` if (a) the post is in a different
    channel than the new post or (b) the post is itself a reply (it
    requires the *thread root*, not an intermediate reply). Both come
    back as the same opaque ``Invalid RootId parameter`` 400 — the
    agent has no way to tell what's wrong from the error alone, so
    we fix it client-side.

    Returns ``(resolved_root_id, note)``:
    - ``resolved_root_id`` is the post id to actually pass to MM
      (the thread root, possibly different from the input).
    - Empty string means "drop root_id and post as a new top-level
      message" — happens when the input post is in a different
      channel or doesn't exist. The ``note`` is appended to the
      tool's success string so the agent knows we silently ignored
      its root_id rather than honoring it.
    """
    try:
        post = await _get(session, cfg.url, f"/api/v4/posts/{root_id}")
    except Exception:
        return "", f" (note: root_id {root_id[:10]}... not found, posted as new message)"
    if post.get("channel_id") != target_channel_id:
        return "", (
            f" (note: root_id {root_id[:10]}... is in a different channel, "
            f"posted as new message)"
        )
    parent_root = post.get("root_id") or ""
    if parent_root:
        # Walk one hop to the actual root. MM only allows two-level
        # threads (root → replies), so one hop is enough.
        return parent_root, f" (note: rerooted to {parent_root[:10]}...)"
    return root_id, ""


# ── File upload ───────────────────────────────────────────────────────────────


def _safe_workspace_path(workspace: str, path: str) -> Path:
    """Resolve ``path`` relative to the workspace root, refusing
    anything that would escape the workspace via ``..`` or absolute
    paths. Returns the absolute ``Path`` on the host.
    """
    wsp = Path(workspace).resolve()
    candidate = (wsp / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
    try:
        candidate.relative_to(wsp)
    except ValueError as exc:
        raise RuntimeError(
            f"refusing to access {candidate} — outside workspace {wsp}"
        ) from exc
    if not candidate.is_file():
        raise RuntimeError(f"file not found: {candidate}")
    return candidate


async def _upload_file_bytes(
    session: aiohttp.ClientSession,
    cfg: ToolsConfig,
    channel_id: str,
    file_path: Path,
) -> list[str]:
    """POST /api/v4/files, return the list of file_ids to attach.

    Mattermost's file-upload handler validates ``channel_id`` AND
    ``filename`` as URL query parameters *before* parsing the
    multipart body — recent server versions return
    ``api.context.invalid_url_param.app_error`` if either field is
    only in the form data. We put both in query + form so the call
    works across server versions.
    """
    data = aiohttp.FormData()
    data.add_field("channel_id", channel_id)
    data.add_field(
        "files",
        file_path.read_bytes(),
        filename=file_path.name,
        content_type="application/octet-stream",
    )
    # Don't reuse _post — this one is multipart, no JSON Content-Type.
    headers = {"Authorization": f"Bearer {cfg.token}"}
    async with session.post(
        cfg.url.rstrip("/") + "/api/v4/files",
        params={"channel_id": channel_id, "filename": file_path.name},
        data=data,
        headers=headers,
    ) as resp:
        if resp.status not in (200, 201):
            body = await resp.text()
            raise RuntimeError(_format_http_error("POST", "/api/v4/files", resp.status, body))
        body = await resp.json()
    return [fi["id"] for fi in body.get("file_infos") or []]


# ── Permission proxy ──────────────────────────────────────────────────────────


async def _resolve_owner_dm(
    session: aiohttp.ClientSession,
    cfg: ToolsConfig,
    bot_user_id: str,
) -> tuple[str, str]:
    """Return (owner_user_id, dm_channel_id). Raises if no owner
    configured."""
    if not cfg.owner_username:
        raise RuntimeError(
            "permission proxy needs --owner-username on the MCP server"
        )
    user = await _get(
        session, cfg.url, f"/api/v4/users/username/{cfg.owner_username}",
    )
    channel = await _post(
        session, cfg.url, "/api/v4/channels/direct",
        [bot_user_id, user["id"]],
    )
    return user["id"], channel["id"]


async def _await_permission_reply(
    session: aiohttp.ClientSession,
    cfg: ToolsConfig,
    thread_root_id: str,
    owner_user_id: str,
    since_ts: int,
) -> Optional[bool]:
    """Poll the permission-request THREAD for a reply from the
    owner. Each request is posted as a top-level DM message that
    becomes a thread root; the owner replies in-thread with y/n.
    Threading keeps concurrent tool approvals from cross-
    contaminating — reply to request A stays in A's thread and
    never gets credited to request B.

    Returns True on approval, False on denial, None on timeout.
    First message from owner starting with y/Y/approve means allow;
    anything else means deny.
    """
    deadline = time.time() + cfg.permission_timeout_seconds
    while time.time() < deadline:
        try:
            data = await _get(
                session, cfg.url,
                f"/api/v4/posts/{thread_root_id}/thread",
            )
        except Exception as exc:
            logger.warning("permission poll failed: %s", exc)
            await asyncio.sleep(2)
            continue
        posts = data.get("posts") or {}
        order = data.get("order") or []
        for pid in order:
            post = posts.get(pid) or {}
            if post.get("user_id") != owner_user_id:
                continue
            # Thread endpoint may include the request's root post if
            # its parent matched — but the root was posted by the
            # bot, not the owner, so the user_id filter drops it.
            # Still, ``since_ts`` is a final safety net against any
            # cached pre-request message showing up here.
            create_at = int(post.get("create_at", 0))  # ms
            if create_at // 1000 <= since_ts:
                continue
            msg = (post.get("message") or "").strip().lower()
            if not msg:
                continue
            return msg[0] in ("y", "a")  # y/yes/approve -> allow
        await asyncio.sleep(2)
    return None


# ── FastMCP server factory ────────────────────────────────────────────────────


def build_server(cfg: ToolsConfig) -> FastMCP:
    """Return a configured FastMCP server. Shared between the
    stdio entry point and in-process SDK registration."""
    mcp = FastMCP("puffo")

    # Lazy-fetched once; claude CLI sessions are long-lived so the
    # first tool call primes this, subsequent calls reuse it.
    cached_me: dict[str, str] = {}

    async def _me(session: aiohttp.ClientSession) -> dict[str, str]:
        if not cached_me:
            me = await _get_me(session, cfg)
            cached_me["id"] = me["id"]
            cached_me["username"] = me.get("username", "")
        return cached_me

    @mcp.tool()
    async def whoami() -> str:
        """Return your own bot identity. Useful when you need to
        recognise @-mentions of yourself or introduce yourself.

        Reports the Mattermost user record (username, user_id,
        first_name, last_name, nickname, email) plus your AIAgent
        record (display_name, team_name) when present. The values
        come from /api/v4/users/me + /api/v4/aiagents/{user_id}.
        """
        async with aiohttp.ClientSession(headers=_headers(cfg.token)) as session:
            me = await _get(session, cfg.url, "/api/v4/users/me")
            lines = [
                f"username:     @{me.get('username', '?')}",
                f"user_id:      {me.get('id', '?')}",
                f"first_name:   {me.get('first_name', '') or '(empty)'}",
                f"last_name:    {me.get('last_name', '') or '(empty)'}",
                f"nickname:     {me.get('nickname', '') or '(empty)'}",
                f"email:        {me.get('email', '') or '(empty)'}",
                f"is_bot:       {me.get('is_bot', False)}",
            ]
            try:
                agent = await _get(
                    session, cfg.url, f"/api/v4/aiagents/{me['id']}",
                )
                lines.extend([
                    f"display_name: {agent.get('display_name', '') or '(empty)'}",
                    f"team_name:    {agent.get('team_name', '') or '(empty)'}",
                ])
            except Exception:
                # Bot might not have an AIAgent record (e.g. legacy
                # bot tokens predating the registry). Skip silently.
                pass
            return "\n".join(lines)

    @mcp.tool()
    async def send_message(channel: str, text: str, root_id: str = "") -> str:
        """Post a message to a Puffo.ai channel or DM a user.

        channel: '@username' for a DM, '#channel-name' for a named
            channel in the bot's team, or a raw 26-char channel id.
        text: message body (Markdown supported).
        root_id: optional — reply inside an existing post's thread.
        """
        async with aiohttp.ClientSession(headers=_headers(cfg.token)) as session:
            me = await _me(session)
            channel_id = await _resolve_channel(session, cfg, me["id"], channel)
            payload: dict[str, Any] = {"channel_id": channel_id, "message": text}
            note = ""
            if root_id:
                resolved, note = await _resolve_root_id(
                    session, cfg, root_id, channel_id,
                )
                if resolved:
                    payload["root_id"] = resolved
            post = await _post(session, cfg.url, "/api/v4/posts", payload)
            return f"posted {post.get('id', '?')} to {channel}{note}"

    @mcp.tool()
    async def upload_file(path: str, channel: str, caption: str = "") -> str:
        """Upload a file from your workspace to a Puffo.ai channel.

        path: workspace-relative path to the file (e.g. 'report.pdf').
            Absolute paths must stay inside your workspace dir.
        channel: same channel ref syntax as send_message.
        caption: optional text posted alongside the file.
        """
        abs_path = _safe_workspace_path(cfg.workspace, path)
        async with aiohttp.ClientSession(headers=_headers(cfg.token)) as session:
            me = await _me(session)
            channel_id = await _resolve_channel(session, cfg, me["id"], channel)
            file_ids = await _upload_file_bytes(session, cfg, channel_id, abs_path)
            payload = {
                "channel_id": channel_id,
                "message": caption,
                "file_ids": file_ids,
            }
            post = await _post(session, cfg.url, "/api/v4/posts", payload)
            return (
                f"uploaded {abs_path.name} ({len(file_ids)} file) to {channel}, "
                f"post {post.get('id', '?')}"
            )

    @mcp.tool()
    async def list_channels() -> str:
        """Return the list of channels the bot is a member of. Each
        entry shows the channel id, type, and display name. Useful
        when you want to address a channel but don't know its name."""
        async with aiohttp.ClientSession(headers=_headers(cfg.token)) as session:
            me = await _me(session)
            channels = await _get(
                session, cfg.url,
                f"/api/v4/users/{me['id']}/channels",
            )
            rows = []
            for c in channels or []:
                rows.append(
                    f"- {c.get('id')} [{c.get('type')}] "
                    f"{c.get('display_name') or c.get('name')}"
                )
            return "\n".join(rows) or "(no channels)"

    @mcp.tool()
    async def list_channel_members(channel: str) -> str:
        """List the members of a channel (by name or id).

        Returns one line per member: `username (human|bot)`. Good for
        figuring out who's present before you @-mention someone.
        """
        async with aiohttp.ClientSession(headers=_headers(cfg.token)) as session:
            me = await _me(session)
            channel_id = await _resolve_channel(session, cfg, me["id"], channel)
            members = await _get(
                session, cfg.url,
                f"/api/v4/channels/{channel_id}/members?per_page=200",
            )
            rows = []
            for m in members or []:
                try:
                    user = await _get(
                        session, cfg.url,
                        f"/api/v4/users/{m['user_id']}",
                    )
                except Exception:
                    continue
                kind = "bot" if user.get("is_bot") else "human"
                rows.append(f"- {user.get('username', '?')} ({kind})")
            return "\n".join(rows) or "(empty channel)"

    @mcp.tool()
    async def get_channel_history(channel: str, limit: int = 20) -> str:
        """Fetch the last N posts in a channel (default 20, max 200).

        Each line is `<iso-ts>  @<sender> (<type>): <text>`. Attached
        filenames are appended to the line. Useful for catching up on
        a conversation before deciding how to reply.
        """
        limit = max(1, min(int(limit), 200))
        async with aiohttp.ClientSession(headers=_headers(cfg.token)) as session:
            me = await _me(session)
            channel_id = await _resolve_channel(session, cfg, me["id"], channel)
            data = await _get(
                session, cfg.url,
                f"/api/v4/channels/{channel_id}/posts?per_page={limit}",
            )
            posts = data.get("posts") or {}
            order = data.get("order") or []
            # Mattermost returns order newest-first; reverse so the
            # agent reads in chronological order.
            rows = []
            for pid in reversed(order):
                post = posts.get(pid) or {}
                sender_id = post.get("user_id", "")
                try:
                    user = await _get(
                        session, cfg.url, f"/api/v4/users/{sender_id}",
                    )
                except Exception:
                    user = {}
                uname = user.get("username", sender_id[:8] if sender_id else "?")
                kind = "bot" if user.get("is_bot") else "human"
                ts = _ts_to_iso(int(post.get("create_at", 0) or 0))
                text = (post.get("message", "") or "").replace("\n", " ")
                file_ids = post.get("file_ids") or []
                line = f"{ts}  @{uname} ({kind}): {text}"
                if file_ids:
                    names = []
                    for fid in file_ids:
                        try:
                            info = await _get(
                                session, cfg.url, f"/api/v4/files/{fid}/info",
                            )
                            names.append(info.get("name", fid))
                        except Exception:
                            names.append(fid)
                    line += f"  [files: {', '.join(names)}]"
                rows.append(line)
            return "\n".join(rows) or "(no posts)"

    @mcp.tool()
    async def fetch_channel_files(channel: str, limit: int = 20) -> str:
        """Back-fill file attachments from recent channel history into
        your workspace so your Read tool can open them.

        Walks the last N posts, downloads every attached file to
        ``attachments/<post_id>/<filename>`` inside your workspace,
        and returns one line per downloaded file.
        """
        limit = max(1, min(int(limit), 200))
        workspace = Path(cfg.workspace)
        async with aiohttp.ClientSession(headers=_headers(cfg.token)) as session:
            me = await _me(session)
            channel_id = await _resolve_channel(session, cfg, me["id"], channel)
            data = await _get(
                session, cfg.url,
                f"/api/v4/channels/{channel_id}/posts?per_page={limit}",
            )
            posts = data.get("posts") or {}
            order = data.get("order") or []
            saved: list[str] = []
            for pid in reversed(order):
                post = posts.get(pid) or {}
                file_ids = post.get("file_ids") or []
                if not file_ids:
                    continue
                dest_root = workspace / "attachments" / pid
                try:
                    dest_root.mkdir(parents=True, exist_ok=True)
                except OSError:
                    continue
                for fid in file_ids:
                    try:
                        info = await _get(
                            session, cfg.url, f"/api/v4/files/{fid}/info",
                        )
                        filename = os.path.basename(info.get("name") or fid) or fid
                        dest = dest_root / filename
                        if dest.exists():
                            saved.append(f"attachments/{pid}/{filename} (cached)")
                            continue
                        async with session.get(
                            f"{cfg.url.rstrip('/')}/api/v4/files/{fid}",
                        ) as resp:
                            if resp.status != 200:
                                continue
                            data_bytes = await resp.read()
                        dest.write_bytes(data_bytes)
                        saved.append(f"attachments/{pid}/{filename}")
                    except Exception:
                        continue
            return "\n".join(saved) or "(no attachments in recent history)"

    @mcp.tool()
    async def get_post(post_ref: str) -> str:
        """Fetch one post by its id or Mattermost permalink URL.

        post_ref: either a raw 26-char post id, or a URL of the form
            ``https://server/teamname/pl/<postid>`` (a permalink).
        Returns sender + timestamp + message text + attached filenames.
        """
        post_id = _parse_post_ref(post_ref)
        async with aiohttp.ClientSession(headers=_headers(cfg.token)) as session:
            post = await _get(session, cfg.url, f"/api/v4/posts/{post_id}")
            try:
                user = await _get(
                    session, cfg.url, f"/api/v4/users/{post.get('user_id', '')}",
                )
            except Exception:
                user = {}
            uname = user.get("username", "?")
            kind = "bot" if user.get("is_bot") else "human"
            ts = _ts_to_iso(int(post.get("create_at", 0) or 0))
            text = post.get("message", "") or ""
            file_ids = post.get("file_ids") or []
            lines = [
                f"post_id: {post_id}",
                f"sender: @{uname} ({kind})",
                f"timestamp: {ts}",
                f"message:\n{text}",
            ]
            if file_ids:
                names = []
                for fid in file_ids:
                    try:
                        info = await _get(
                            session, cfg.url, f"/api/v4/files/{fid}/info",
                        )
                        names.append(info.get("name", fid))
                    except Exception:
                        names.append(fid)
                lines.append(f"attachments: {', '.join(names)}")
            return "\n".join(lines)

    @mcp.tool()
    async def get_user_info(username: str) -> str:
        """Look up a user by their @-handle.

        Returns username, display name, email, and bot/human type.
        Useful when you're about to DM someone and want to confirm
        who they are, or when you want to know if a name in a message
        is a bot.
        """
        username = (username or "").lstrip("@").strip()
        if not username:
            raise RuntimeError("username is required")
        async with aiohttp.ClientSession(headers=_headers(cfg.token)) as session:
            user = await _get(
                session, cfg.url, f"/api/v4/users/username/{username}",
            )
        kind = "bot" if user.get("is_bot") else "human"
        display = (
            f"{user.get('first_name', '')} {user.get('last_name', '')}"
        ).strip() or user.get("nickname") or user.get("username", "")
        return (
            f"username: {user.get('username', '')}\n"
            f"display: {display}\n"
            f"email: {user.get('email', '')}\n"
            f"type: {kind}"
        )

    @mcp.tool()
    async def reload_system_prompt() -> str:
        """Rebuild your system prompt from disk and restart your
        claude subprocess so fresh edits take effect on your next
        message.

        **When to use:**
        - You just edited your project-level ``/workspace/CLAUDE.md``
          (or ``/workspace/.claude/CLAUDE.md``) and want it in your
          next system prompt.
        - You wrote a new ``memory/*.md`` file under your agent dir
          and want it folded into the managed layer immediately.
        - You edited ``profile.md`` and want the new role live.

        **What happens:**
        1. Your current reply goes through as normal (the subprocess
           stays alive for this turn).
        2. On the NEXT incoming message, the daemon regenerates your
           managed ``~/.claude/CLAUDE.md`` from shared primer +
           profile + memory, closes your claude subprocess, and
           spawns a new one with ``--resume`` pointing at your
           existing session — so conversation history is preserved
           while the system prompt is fresh.

        No arguments. Returns a short confirmation.
        """
        flag_path = Path(cfg.workspace) / ".puffoagent" / "reload.flag"
        try:
            flag_path.parent.mkdir(parents=True, exist_ok=True)
            flag_path.write_text(
                f'{{"requested_at": {int(time.time())}}}\n',
                encoding="utf-8",
            )
        except OSError as exc:
            raise RuntimeError(f"could not write reload flag: {exc}") from exc
        return (
            "reload requested — your system prompt will be rebuilt and "
            "your claude subprocess restarted before your next message "
            "(conversation history preserved via --resume)."
        )

    # ── Skill + MCP install / refresh tools ──────────────────────────────────
    #
    # These all assume Claude Code's skills-dir layout + --resume
    # session protocol. Under a different harness (e.g. hermes) the
    # side effects land on paths nobody reads, so we reject at the
    # tool boundary instead of silently writing files that mislead
    # the agent into thinking they'll take effect.

    def _require_claude_code(tool: str) -> None:
        if cfg.harness and cfg.harness != "claude-code":
            raise RuntimeError(
                f"{tool} is only supported under the claude-code "
                f"harness (this agent is using {cfg.harness!r}). "
                f"Skills and project-scope MCP registrations use "
                f"Claude Code's own file layout and session protocol; "
                f"under {cfg.harness!r} those files wouldn't be read."
            )

    @mcp.tool()
    async def install_skill(name: str, content: str) -> str:
        """Install a new skill into your project-scope skills dir so
        you can invoke it with ``/<name>`` (or let Claude Code
        auto-load it when the description matches).

        **When to use:** You want to reuse a prompt playbook, checklist,
        or procedure across many turns. Unlike CLAUDE.md content, a
        skill only loads into context when invoked, so it costs almost
        nothing until needed.

        **Effect:** Writes ``<workspace>/.claude/skills/<name>/SKILL.md``
        with the provided ``content`` and drops an
        ``agent-installed.md`` marker alongside. Project-scope is
        owned by you; host syncs never touch it. Call ``refresh()``
        afterwards so your next turn's claude subprocess picks it up.

        Args:
          name: slug (lowercase letters/digits/hyphens, max 64, can't
            start with a hyphen).
          content: the full SKILL.md body, typically starting with
            ``---``-delimited YAML frontmatter (``name:``,
            ``description:``) followed by markdown instructions.
            See https://code.claude.com/docs/en/skills for the format.
        """
        _require_claude_code("install_skill")
        dst = _install_skill(Path(cfg.workspace), name, content)
        return (
            f"installed skill {name!r} at project scope ({dst}). "
            "Call refresh() so your next turn picks it up."
        )

    @mcp.tool()
    async def uninstall_skill(name: str) -> str:
        """Remove a skill you previously installed.

        Only agent-installed skills at project scope can be removed.
        System skills (synced from the operator's host) are managed
        off-agent and uninstalling them here would just get overridden
        on the next worker start anyway — this tool refuses to touch
        them so you don't accidentally chase your tail.

        Args:
          name: the skill slug to remove.
        """
        _require_claude_code("uninstall_skill")
        _uninstall_skill(Path(cfg.workspace), name)
        return (
            f"uninstalled skill {name!r}. Call refresh() so your next "
            "turn stops seeing it."
        )

    @mcp.tool()
    async def list_skills() -> str:
        """List every skill available to you, tagged by scope.

        ``[system]`` entries live at user scope and are managed by the
        operator. ``[agent]`` entries live at project scope and were
        installed by you (or a previous session of you) via
        ``install_skill``.

        When the same name appears at both scopes, Claude Code applies
        its own precedence: personal > project. list_skills shows both
        so you can see what might shadow what.
        """
        entries = _list_skills(Path(cfg.workspace), Path.home())
        if not entries:
            return "(no skills installed)"
        return "\n".join(
            f"[{scope}]{' ' if scope == 'agent' else ''} {name}"
            for scope, name in entries
        )

    @mcp.tool()
    async def install_mcp_server(
        name: str,
        command: str,
        args: Optional[list[str]] = None,
        env: Optional[dict[str, str]] = None,
    ) -> str:
        """Register a new stdio MCP server at your project scope so
        you gain its tools on your next turn.

        **Distribution model:** MCP servers typically ship as npm or
        Python packages invoked via ``npx`` / ``uvx``. You don't
        install the package — the command fetches + caches it at
        spawn time. Both ``npx`` and ``uvx`` are available inside the
        runtime.

        **Effect:** Writes ``<workspace>/.mcp.json`` with your entry
        merged into ``mcpServers.<name>``. The file is project-scope
        per Claude Code's documented layout
        (https://code.claude.com/docs/en/mcp), separate from the
        user-scope ``.claude.json`` host syncs manage.

        **Restrictions:** the command must resolve inside the runtime.
        On ``cli-docker`` host paths (``/Users/...``,
        ``/home/<someone>/...``, Windows drive letters) are rejected
        because they won't exist inside the container. On
        ``cli-local`` the agent runs on the host so the check is
        skipped — any path the host user can execute is fair game.

        After install, call ``refresh()`` so the claude subprocess
        respawns and connects to the new server.

        Args:
          name: short slug for the server (shown in ``/mcp``).
          command: executable — ``npx``, ``uvx``, ``python3``, or an
            absolute path inside the runtime.
          args: argument list (default: []). Example for an npm MCP:
            ``["-y", "@scope/mcp-package"]``.
          env: environment variables to pass the server (default: {}).
            Use this for per-server API keys.
        """
        # cli-local: agent runs on the host, so host paths resolve —
        # skip the host-local rejection (it would fire false-positive
        # on workspace-script MCPs under /home/<operator>/...).
        _require_claude_code("install_mcp_server")
        check_host_local = cfg.runtime_kind != "cli-local"
        path = _install_mcp_server(
            Path(cfg.workspace), name, command, args, env,
            check_host_local=check_host_local,
        )
        return (
            f"registered MCP server {name!r} at project scope ({path}). "
            "Call refresh() so the claude subprocess respawns and "
            "connects to it."
        )

    @mcp.tool()
    async def uninstall_mcp_server(name: str) -> str:
        """Remove an MCP server you previously registered.

        Only project-scope entries (the ones in ``<workspace>/.mcp.json``)
        can be removed. System MCPs live in the user-scope
        ``.claude.json`` and are managed by the operator.

        Args:
          name: the server name to remove.
        """
        _require_claude_code("uninstall_mcp_server")
        _uninstall_mcp_server(Path(cfg.workspace), name)
        return (
            f"removed MCP server {name!r}. Call refresh() so the claude "
            "subprocess respawns without it."
        )

    @mcp.tool()
    async def list_mcp_servers() -> str:
        """List every MCP server available to you, tagged by scope.

        ``[system]`` entries come from the operator's user-scope
        ``~/.claude.json`` (synced in at worker start). ``[agent]``
        entries come from your own ``<workspace>/.mcp.json`` and
        were registered via ``install_mcp_server``.

        When the same name exists at both scopes, Claude Code applies
        its precedence: local > project > user. list_mcp_servers shows
        both so you can see what might shadow what.
        """
        entries = _list_mcp_servers(Path(cfg.workspace), Path.home())
        if not entries:
            return "(no MCP servers registered)"
        return "\n".join(
            f"[{scope}]{' ' if scope == 'agent' else ''} {n}"
            for scope, n in entries
        )

    @mcp.tool()
    async def refresh(model: Optional[str] = None) -> str:
        """Respawn your claude subprocess so it re-discovers skills,
        MCP servers, and optionally switches to a new model — without
        regenerating CLAUDE.md.

        **When to use:**
        - You just called ``install_skill`` / ``install_mcp_server``
          (or their uninstall siblings) and want the change live.
        - You want to switch the runtime model mid-conversation.

        **Difference from ``reload_system_prompt``:** reload rebuilds
        your managed ``~/.claude/CLAUDE.md`` from primer + profile +
        memory before restart. refresh only restarts — lighter, and
        appropriate when you haven't edited profile/memory.

        **What happens:**
        1. Your current reply finishes normally.
        2. On your NEXT incoming message, the daemon kills your claude
           subprocess (optionally after mutating the model flag) and
           respawns it with ``--resume`` pointing at the existing
           session — so conversation history is preserved while config
           is re-read fresh.

        Args:
          model: optional model override. Pass a model id like
            ``claude-opus-4-6`` / ``claude-sonnet-4-6``, or empty
            string to clear back to the daemon default. Omit to keep
            the current model.
        """
        _require_claude_code("refresh")
        _write_refresh_flag(Path(cfg.workspace), model)
        tail = f" (model override: {model!r})" if model is not None else ""
        return (
            "refresh requested — your claude subprocess will respawn "
            "before your next message" + tail + ". Conversation history "
            "is preserved via --resume; CLAUDE.md is not regenerated "
            "(use reload_system_prompt for that)."
        )

    @mcp.tool()
    async def approve_permission(
        tool_name: str, input: dict[str, Any],
    ) -> dict[str, Any]:
        """Claude Code permission-prompt callback. The claude CLI
        invokes this when it wants permission to run a tool and
        was launched with --permission-prompt-tool pointing here.
        Forward the request to the owner on Mattermost, poll for
        a reply, return the decision.

        Returns a PermissionResult-shaped dict so the CLI knows
        how to proceed: {'behavior': 'allow'|'deny', ...}.
        """
        async with aiohttp.ClientSession(headers=_headers(cfg.token)) as session:
            me = await _me(session)
            try:
                owner_id, dm = await _resolve_owner_dm(session, cfg, me["id"])
            except Exception as exc:
                logger.warning("permission: cannot reach owner: %s", exc)
                return {
                    "behavior": "deny",
                    "message": f"permission proxy misconfigured: {exc}",
                }
            summary = _summarise_tool_input(input)
            request_at = int(time.time())
            # Post the request as a top-level DM; the returned post
            # id becomes the thread root. The user replies IN THE
            # THREAD, which scopes the decision to this specific
            # request — when claude fires several tool approvals in
            # parallel (or back-to-back), each has its own thread
            # and the replies can't be miscredited.
            posted = await _post(
                session, cfg.url, "/api/v4/posts",
                {
                    "channel_id": dm,
                    "message": (
                        f"🔐 **agent `{cfg.agent_id}` wants to run `{tool_name}`**\n\n"
                        f"{summary}\n\n"
                        f"**Reply in this thread** with `y` to approve or "
                        f"`n` to deny (times out in "
                        f"{int(cfg.permission_timeout_seconds)}s)."
                    ),
                },
            )
            thread_root = posted.get("id", "")
            if not thread_root:
                logger.warning(
                    "permission: server did not return a post id for the "
                    "request — falling back to deny"
                )
                return {
                    "behavior": "deny",
                    "message": "permission proxy could not post request",
                }
            decision = await _await_permission_reply(
                session, cfg, thread_root, owner_id, request_at,
            )
        if decision is True:
            return {"behavior": "allow", "updatedInput": input}
        if decision is False:
            return {"behavior": "deny", "message": "owner denied the request"}
        return {"behavior": "deny", "message": "permission request timed out"}

    return mcp


def _summarise_tool_input(data: Any, limit: int = 400) -> str:
    """Short, human-readable summary of a tool_input dict for
    the permission prompt posted to the owner.
    """
    if isinstance(data, dict):
        parts = []
        for k, v in data.items():
            s = str(v)
            if len(s) > 120:
                s = s[:120] + "…"
            parts.append(f"- **{k}**: `{s}`")
        text = "\n".join(parts)
    else:
        text = f"`{str(data)[:limit]}`"
    if len(text) > limit:
        text = text[:limit] + "…"
    return text or "(no input)"


# ── Stdio entry point ────────────────────────────────────────────────────────


def _cfg_from_args() -> ToolsConfig:
    """Assemble ToolsConfig from argv and PUFFO_* env vars. Env
    vars provide defaults so docker can pass secrets via ``-e``
    without exposing them on the command line."""
    parser = argparse.ArgumentParser(description="Puffo.ai MCP tools server")
    parser.add_argument("--agent-id", default=os.environ.get("PUFFO_AGENT_ID", ""))
    parser.add_argument("--url", default=os.environ.get("PUFFO_URL", ""))
    parser.add_argument("--token", default=os.environ.get("PUFFO_BOT_TOKEN", ""))
    parser.add_argument("--workspace", default=os.environ.get("PUFFO_WORKSPACE", "/workspace"))
    parser.add_argument("--team", default=os.environ.get("PUFFO_TEAM", ""))
    parser.add_argument(
        "--owner-username",
        default=os.environ.get("PUFFO_OWNER_USERNAME", ""),
    )
    parser.add_argument(
        "--permission-timeout",
        type=float,
        default=float(os.environ.get("PUFFO_PERMISSION_TIMEOUT", "300")),
    )
    parser.add_argument(
        "--runtime-kind",
        default=os.environ.get("PUFFO_RUNTIME_KIND", ""),
        choices=("", "chat-local", "sdk-local", "cli-local", "cli-docker"),
        help="Which adapter spawned this server. Gates runtime-aware "
             "checks like the host-local-command rejection in "
             "install_mcp_server.",
    )
    parser.add_argument(
        "--harness",
        default=os.environ.get("PUFFO_HARNESS", ""),
        choices=("", "claude-code", "hermes", "gemini-cli"),
        help="Which agent engine is running. Tools that only make "
             "sense under Claude Code (install_skill / refresh / "
             "install_mcp_server) return a clear error under "
             "other harnesses rather than writing to paths Claude "
             "Code owns.",
    )
    args = parser.parse_args()

    missing = [
        f for f, v in (
            ("--agent-id", args.agent_id),
            ("--url", args.url),
            ("--token", args.token),
        ) if not v
    ]
    if missing:
        parser.error(f"missing required: {', '.join(missing)}")

    return ToolsConfig(
        agent_id=args.agent_id,
        url=args.url,
        token=args.token,
        workspace=args.workspace,
        team=args.team,
        owner_username=args.owner_username,
        permission_timeout_seconds=args.permission_timeout,
        runtime_kind=args.runtime_kind,
        harness=args.harness,
    )


def main() -> None:
    """Stdio MCP server entry point. claude-code (cli-local /
    cli-docker) spawns this via ``--mcp-config`` and talks to it
    over stdin/stdout.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=os.sys.stderr,  # stdout belongs to MCP protocol
    )
    cfg = _cfg_from_args()
    server = build_server(cfg)
    logger.info(
        "puffo MCP server starting: agent=%s url=%s workspace=%s team=%s",
        cfg.agent_id, cfg.url, cfg.workspace, cfg.team or "(none)",
    )
    # FastMCP.run() blocks; suppress the "keyboard interrupt" noise
    # so docker logs from the subprocess stay clean.
    with contextlib.suppress(KeyboardInterrupt):
        server.run()


if __name__ == "__main__":
    main()
