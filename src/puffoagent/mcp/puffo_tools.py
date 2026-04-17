"""Self-contained MCP server exposing Puffo.ai tools to AI agents.

Runs as a stdio subprocess spawned by the claude CLI (or
claude-agent-sdk), registered via ``--mcp-config`` or
``ClaudeAgentOptions.mcp_servers``. The server needs no puffoagent
import surface at runtime — only ``aiohttp`` and ``mcp`` — so we can
run the same file on the host (cli-local), inside the cli-docker
container (bind-mounted), or inside the SDK adapter's process.

Tools exposed (prefixed ``mcp__puffo__`` when invoked from claude):

    send_message(channel, text, root_id="")
        Post to a channel or DM by name/id. Returns the new post id.

    upload_file(path, channel, caption="")
        Upload a workspace-relative file to a channel + post it.

    list_channels(team="")
        List channels the bot is a member of. Lightweight discovery.

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
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import aiohttp
from mcp.server.fastmcp import FastMCP

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
    """POST /api/v4/files, return the list of file_ids to attach."""
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
        data=data, headers=headers,
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
    dm_channel_id: str,
    owner_user_id: str,
    since_ts: int,
) -> Optional[bool]:
    """Poll the DM for a reply from the owner after ``since_ts``.
    Returns True on approval, False on denial, None on timeout.

    The protocol is intentionally dumb: first message from owner
    starting with y/Y/approve means allow; anything else (n/no/deny)
    means deny. Timeout after ``permission_timeout_seconds``.
    """
    deadline = time.time() + cfg.permission_timeout_seconds
    while time.time() < deadline:
        try:
            # ``per_page=5`` — enough to catch the reply without
            # dragging in long history.
            data = await _get(
                session, cfg.url,
                f"/api/v4/channels/{dm_channel_id}/posts?per_page=5",
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
            if root_id:
                payload["root_id"] = root_id
            post = await _post(session, cfg.url, "/api/v4/posts", payload)
            return f"posted {post.get('id', '?')} to {channel}"

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
            await _post(
                session, cfg.url, "/api/v4/posts",
                {
                    "channel_id": dm,
                    "message": (
                        f"🔐 **agent `{cfg.agent_id}` wants to run `{tool_name}`**\n\n"
                        f"{summary}\n\n"
                        f"reply `y` to approve, `n` to deny "
                        f"(times out in {int(cfg.permission_timeout_seconds)}s)"
                    ),
                },
            )
            decision = await _await_permission_reply(
                session, cfg, dm, owner_id, request_at,
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
