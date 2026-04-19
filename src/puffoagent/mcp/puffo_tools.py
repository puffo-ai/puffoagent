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
import logging
import os
import re
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


# ── HTTP helpers ──────────────────────────────────────────────────────────────


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


async def _get(session: aiohttp.ClientSession, url: str, path: str) -> Any:
    async with session.get(url.rstrip("/") + path) as resp:
        if resp.status != 200:
            body = await resp.text()
            raise RuntimeError(f"GET {path} -> {resp.status}: {body[:200]}")
        return await resp.json()


async def _post(
    session: aiohttp.ClientSession, url: str, path: str, payload: Any,
) -> Any:
    async with session.post(url.rstrip("/") + path, json=payload) as resp:
        if resp.status not in (200, 201):
            body = await resp.text()
            raise RuntimeError(f"POST {path} -> {resp.status}: {body[:200]}")
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
            raise RuntimeError(f"file upload -> {resp.status}: {body[:200]}")
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
