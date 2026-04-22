import asyncio
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
import aiohttp

from ._logging import agent_logger


_MENTION_RE = re.compile(r"@([a-zA-Z0-9._-]+)")

# How often the puffoagent-specific heartbeat is sent to /aiagents/me/heartbeat.
# Drives AIAgent.Status (server-side heartbeat-staleness check).
AI_AGENT_HEARTBEAT_INTERVAL = 30

# How often we push the user-level "online" status for the bot account
# — distinct from the AIAgent heartbeat above. The webapp's presence
# dot for users (including bots) reads from the standard Status
# table; without this, bots appear offline even while the daemon is
# happily running. The server's PUT /users/{id}/status handler DOES
# set the status before its 404-prone re-read kicks in, so we
# deliberately swallow 404s here.
STATUS_HEARTBEAT_INTERVAL = 30

# Priority tiers for the per-agent message queue. Lower number = higher
# priority; the consumer pulls lowest first. The order matches what
# matters most to the user:
#   1. Human addressed us directly — always first.
#   2. Another agent addressed us directly (e.g., a handoff from Max).
#   3. Human talking in a shared channel, not addressing us.
#   4. Another agent talking in a shared channel, not addressing us.
#   5. Mattermost system posts (join/leave/invite notifications).
# "Addressed us" = DM or explicit @mention.
PRIORITY_MENTIONED_HUMAN = 1
PRIORITY_MENTIONED_BOT = 2
PRIORITY_HUMAN = 3
PRIORITY_BOT = 4
PRIORITY_SYSTEM = 5


def _compute_priority(direct: bool, sender_is_bot: bool, is_system: bool) -> int:
    if is_system:
        return PRIORITY_SYSTEM
    if direct and not sender_is_bot:
        return PRIORITY_MENTIONED_HUMAN
    if direct and sender_is_bot:
        return PRIORITY_MENTIONED_BOT
    if not sender_is_bot:
        return PRIORITY_HUMAN
    return PRIORITY_BOT


# Cap follow-up context per turn so a long-running thread doesn't
# explode the user message. Most useful info is the *latest* posts,
# so when the count exceeds this, we keep the most recent N.
FOLLOWUP_CONTEXT_LIMIT = 20


def _ms_to_iso(ms: int) -> str:
    """Mattermost timestamps are ms-since-epoch ints. Render as ISO
    8601 in UTC for the agent's user message — both unambiguous and
    sortable by the model.
    """
    if not ms:
        return ""
    try:
        return datetime.fromtimestamp(
            ms / 1000, tz=timezone.utc,
        ).isoformat(timespec="seconds")
    except (ValueError, OSError):
        return ""


class MattermostClient:
    def __init__(
        self,
        url: str,
        token: str,
        profile_name: str = "default",
        file_server_url: str = "",
        agent_id: str = "",
        workspace_dir: str = "",
        team_name: str = "",
    ):
        self.url = url.rstrip("/")
        self.token = token
        self.profile_name = profile_name
        self.file_server_url = file_server_url
        self.team_name = team_name
        # Populated in ``listen()`` after ``_get_me`` via
        # ``resolve_team_id``; compared against incoming
        # ``delete_team`` websocket events so the worker can
        # archive itself when its own Puffo space is removed.
        self.team_id: str = ""
        self.ws_url = self.url.replace("http://", "ws://").replace("https://", "wss://") + "/api/v4/websocket"
        self.headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        # Attachments on incoming posts get downloaded under
        # ``<workspace_dir>/attachments/<post_id>/`` so the agent can
        # open them with its normal Read tool. The relative path
        # ``attachments/<post_id>/<filename>`` is what we surface in
        # the user-message preamble — relative so the same string
        # works for SDK/chat-only (cwd=workspace on host) and for
        # cli-docker (cwd=/workspace in container).
        self.workspace_dir = workspace_dir
        self.bot_user_id: str = ""
        self.bot_username: str = ""
        self._ws = None  # active WebSocket connection for typing events
        self._seq = 1
        # Per-listen priority queue + monotonic counter for FIFO
        # ordering within a single priority tier. Reset on each
        # listen() call so a reconnect doesn't drag stale items
        # through — the WS server doesn't replay missed posts on
        # reconnect, so buffering across reconnect is pointless.
        self._queue: asyncio.PriorityQueue | None = None
        self._queue_seq = 0
        self._rpc_handler = None  # set by the agent to serve file RPC calls
        # Called on ``delete_team`` websocket events so the worker can
        # archive its agent if the server just removed its Puffo space.
        # Defensive: server-side cascade of agent deletion should also
        # trigger local archive via the /aiagents sync, but we don't
        # want a stuck session if the cascade is delayed or skipped.
        self._team_deleted_handler = None
        self._background_tasks: list[asyncio.Task] = []
        self.logger = agent_logger(__name__, agent_id)

    async def _get_me(self, session: aiohttp.ClientSession):
        async with session.get(f"{self.url}/api/v4/users/me") as resp:
            data = await resp.json()
            self.bot_user_id = data["id"]
            self.bot_username = data["username"]
            self.logger.info(f"Logged in as @{self.bot_username} ({self.bot_user_id})")

    async def resolve_team_id(
        self, session: aiohttp.ClientSession, team_name: str,
    ) -> str:
        """Look up a team by name. Returns the team id or ``""`` if
        the name is empty / the lookup fails. Used by the worker at
        startup to stash the bot's own team id so a later
        ``delete_team`` websocket event can be matched without a
        second round-trip."""
        if not team_name:
            return ""
        try:
            async with session.get(
                f"{self.url}/api/v4/teams/name/{team_name}",
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    self.logger.warning(
                        "team lookup for %r returned %s: %s",
                        team_name, resp.status, body[:200],
                    )
                    return ""
                data = await resp.json()
                return (data.get("id") or "").strip()
        except Exception as exc:
            self.logger.warning("team lookup for %r failed: %s", team_name, exc)
            return ""

    async def _register_as_ai_agent(self, session: aiohttp.ClientSession):
        """Register with the server's AIAgents table via POST /aiagents/register.
        Called once on startup. Upserts server-side so a restart is idempotent.
        """
        payload = {
            "profile_name": self.profile_name,
            "file_server_url": self.file_server_url,
        }
        async with session.post(f"{self.url}/api/v4/aiagents/register", json=payload) as resp:
            if resp.status in (200, 201):
                self.logger.info("Registered as puffo_ai_agent via /aiagents/register")
            else:
                body = await resp.text()
                self.logger.warning(f"Could not register as AI agent: {resp.status} {body}")

    async def _ai_agent_heartbeat_loop(self, session: aiohttp.ClientSession):
        """POST /aiagents/me/heartbeat every AI_AGENT_HEARTBEAT_INTERVAL seconds.
        Lets the server derive the online/offline dot and gives us a place to
        push `busy` state during response generation in the future.
        """
        while True:
            await asyncio.sleep(AI_AGENT_HEARTBEAT_INTERVAL)
            try:
                async with session.post(
                    f"{self.url}/api/v4/aiagents/me/heartbeat",
                    json={"status": "online"},
                ) as resp:
                    if resp.status not in (200, 201):
                        self.logger.warning(f"Heartbeat failed: {resp.status}")
            except Exception as e:
                self.logger.warning(f"Heartbeat error: {e}")

    async def _set_online(self, session: aiohttp.ClientSession):
        """PUT /users/{id}/status online. The Mattermost handler
        sets the cache/DB status BEFORE its response re-read, so
        the status update lands even when the response itself 404s
        (the server tries to re-fetch a just-created Status row
        that isn't in cache yet for bot users — harmless quirk).
        We swallow 404 at INFO level so logs stay clean.
        """
        if not self.bot_user_id:
            # _get_me hasn't populated this yet; first heartbeat
            # after listen() start runs post-me, so this is a
            # defensive guard for edge cases only.
            return
        payload = {"user_id": self.bot_user_id, "status": "online"}
        try:
            async with session.put(
                f"{self.url}/api/v4/users/{self.bot_user_id}/status",
                json=payload,
            ) as resp:
                if resp.status in (200, 201, 404):
                    # 404 is the known "status set, response re-read
                    # can't find the Status row" path — cosmetic.
                    return
                self.logger.warning(
                    f"Failed to set online status: {resp.status}",
                )
        except Exception as e:
            self.logger.warning(f"Set-online error: {e}")

    async def _status_heartbeat(self, session: aiohttp.ClientSession):
        """Refresh the bot's user-level ``online`` status periodically
        so the webapp's presence dot stays lit. Uses the same
        aiohttp session as the ai_agent heartbeat — no per-call
        session churn.
        """
        while True:
            try:
                await self._set_online(session)
            except Exception as e:
                self.logger.warning(f"Status heartbeat error: {e}")
            await asyncio.sleep(STATUS_HEARTBEAT_INTERVAL)

    async def _fetch_followup_context(
        self,
        session: aiohttp.ClientSession,
        channel_id: str,
        raw_root_id: str,
        primary_post_id: str,
        since_create_at: int,
    ) -> list[dict]:
        """Fetch posts that arrived in the same conversation as the
        primary message, between the primary's timestamp and now.

        Conversation scope:
          - If the primary is a thread reply (``raw_root_id`` set),
            scope = the thread (``GET /posts/{root}/thread``).
          - Otherwise scope = the channel (``GET /channels/{id}/
            posts?since={ms}``).

        The primary post itself and any of our own posts are filtered
        out — the primary is already in the user message, and our own
        replies are already in the conversation log so re-injecting
        them would just confuse the model. Result is sorted oldest
        first and capped at ``FOLLOWUP_CONTEXT_LIMIT``.
        """
        if not since_create_at:
            return []
        if raw_root_id:
            url = f"{self.url}/api/v4/posts/{raw_root_id}/thread"
        else:
            url = (
                f"{self.url}/api/v4/channels/{channel_id}/posts"
                f"?since={since_create_at}"
            )
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    self.logger.warning(
                        f"fetch followup context {resp.status}: {body[:200]}"
                    )
                    return []
                data = await resp.json()
        except Exception as exc:
            self.logger.warning(f"fetch followup context error: {exc}")
            return []

        posts = data.get("posts") or {}
        items: list[dict] = []
        for pid, post in posts.items():
            if not isinstance(post, dict):
                continue
            if pid == primary_post_id:
                continue
            if post.get("user_id") == self.bot_user_id:
                continue
            created = int(post.get("create_at", 0) or 0)
            if created < since_create_at:
                continue
            text = post.get("message", "") or ""
            if not text:
                # Deleted / system-only posts with no body — skip.
                continue
            items.append({
                "id": pid,
                "create_at": created,
                "sender_id": post.get("user_id", ""),
                "text": text,
            })
        items.sort(key=lambda i: i["create_at"])
        if len(items) > FOLLOWUP_CONTEXT_LIMIT:
            items = items[-FOLLOWUP_CONTEXT_LIMIT:]

        # Resolve sender usernames once — agents reading the user
        # message care about the human-friendly @name, not the user
        # id. Reuses the same authenticated session.
        sender_ids = {i["sender_id"] for i in items if i["sender_id"]}
        usernames: dict[str, str] = {}
        for sid in sender_ids:
            try:
                async with session.get(
                    f"{self.url}/api/v4/users/{sid}",
                ) as resp:
                    if resp.status == 200:
                        user = await resp.json()
                        usernames[sid] = user.get("username", sid)
                    else:
                        usernames[sid] = sid
            except Exception:
                usernames[sid] = sid
        for i in items:
            i["sender_username"] = usernames.get(i["sender_id"], i["sender_id"])
            i["timestamp"] = _ms_to_iso(i["create_at"])
        return items

    async def _post_view_receipt(self, session: aiohttp.ClientSession, post_id: str):
        """Send a "this agent has started processing this post" signal.
        Fired when the consumer pulls the post from its priority
        queue, so "viewed by" in the webapp reflects actual attention,
        not just message delivery.

        Reuses the batch endpoint with a single-item list so the
        server-side broadcast path is identical to the former
        behavior — only the timing changes.
        """
        if not post_id:
            return
        try:
            async with session.post(
                f"{self.url}/api/v4/posts/views/batch",
                json={"post_ids": [post_id]},
            ) as resp:
                if resp.status not in (200, 201):
                    body = await resp.text()
                    self.logger.warning(
                        f"View receipt post failed ({post_id}): {resp.status} {body}"
                    )
        except Exception as e:
            self.logger.warning(f"View receipt post error ({post_id}): {e}")

    def set_rpc_handler(self, handler):
        """Register a callable that services `ai_agent_rpc_request` events.

        handler(cmd: str, args: dict) -> (ok: bool, data: dict, error: str|None)
        """
        self._rpc_handler = handler

    def set_team_deleted_handler(self, handler):
        """Register an async callable that fires on ``delete_team``
        websocket events. Receives the ``team_id`` as its only
        argument; returns nothing. Exceptions are caught and logged
        so a misbehaving handler doesn't crash the listen loop.
        """
        self._team_deleted_handler = handler

    async def _handle_rpc_request(self, event: dict):
        data = event.get("data", {}) or {}
        request_id = data.get("request_id", "")
        cmd = data.get("cmd", "")
        args = data.get("args", {}) or {}
        if not request_id:
            return
        if self._rpc_handler is None:
            await self._send_rpc_response(request_id, False, None, "no rpc handler registered")
            return
        try:
            ok, payload, error = await self._rpc_handler(cmd, args)
        except Exception as e:
            self.logger.error(f"RPC handler error: {e}", exc_info=True)
            await self._send_rpc_response(request_id, False, None, str(e))
            return
        await self._send_rpc_response(request_id, ok, payload, error)

    async def _send_rpc_response(self, request_id: str, ok: bool, data, error):
        ws = self._ws
        if ws is None or ws.closed:
            return
        self._seq += 1
        try:
            await ws.send_json({
                "seq": self._seq,
                "action": "ai_agent_rpc_response",
                "data": {
                    "request_id": request_id,
                    "ok": ok,
                    "data": data or {},
                    "error": error or "",
                },
            })
        except Exception as e:
            # Don't let a write to a dying socket bubble out of listen()
            # — the supervisor will reconnect on its own.
            self.logger.warning(f"RPC response send failed: {e}")

    async def send_typing(self, channel_id: str, parent_id: str = ""):
        """Send a typing indicator for this channel via WebSocket."""
        ws = self._ws
        if ws is None or ws.closed:
            return
        try:
            self._seq += 1
            await ws.send_json({
                "seq": self._seq,
                "action": "user_typing",
                "data": {"channel_id": channel_id, "parent_id": parent_id},
            })
        except Exception as e:
            self.logger.warning(f"Failed to send typing indicator: {e}")

    async def post_message(self, channel_id: str, message: str, root_id: str = ""):
        async with aiohttp.ClientSession(headers=self.headers) as session:
            payload = {"channel_id": channel_id, "message": message}
            if root_id:
                payload["root_id"] = root_id
            async with session.post(f"{self.url}/api/v4/posts", json=payload) as resp:
                if resp.status not in (200, 201):
                    self.logger.error(f"Failed to post message: {resp.status} {await resp.text()}")

    async def get_user(self, user_id: str) -> dict:
        async with aiohttp.ClientSession(headers=self.headers) as session:
            async with session.get(f"{self.url}/api/v4/users/{user_id}") as resp:
                return await resp.json()

    async def _resolve_mentions(
        self, session: aiohttp.ClientSession, text: str,
    ) -> list[dict]:
        """Parse ``@name`` mentions from ``text`` and look each up on
        the server so the agent sees who's human vs. bot. Duplicates
        are deduped; unknown names (not valid users) are dropped.

        The bot's own username is included and flagged with
        ``is_self: true``. Paired with the ``@you(<name>)`` rewrite
        in ``_handle_event``, agents get two independent signals
        that they were addressed — one structured, one textual.

        Returns a list of ``{"username": str, "is_bot": bool,
        "is_self": bool}`` in order of first appearance.
        """
        resolved: list[dict] = []
        seen: set[str] = set()
        for m in _MENTION_RE.finditer(text or ""):
            name = m.group(1)
            if name in seen:
                continue
            seen.add(name)
            is_self = name == self.bot_username
            try:
                async with session.get(
                    f"{self.url}/api/v4/users/username/{name}",
                ) as resp:
                    if resp.status != 200:
                        continue
                    user = await resp.json()
            except Exception:
                continue
            resolved.append({
                "username": user.get("username", name),
                "is_bot": bool(user.get("is_bot", False)),
                "is_self": is_self,
            })
        return resolved

    async def _download_attachments(
        self, session: aiohttp.ClientSession, post_id: str, file_ids: list[str],
    ) -> list[str]:
        """Download each attached file to
        ``<workspace>/attachments/<post_id>/<filename>`` and return
        the list of paths RELATIVE to ``workspace_dir`` so the agent
        can resolve them from its own cwd (works identically on host
        and inside the cli-docker container).
        """
        if not file_ids or not self.workspace_dir:
            return []
        dest_root = Path(self.workspace_dir) / "attachments" / post_id
        try:
            dest_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.logger.warning("cannot create %s: %s", dest_root, exc)
            return []

        rel_paths: list[str] = []
        for fid in file_ids:
            try:
                async with session.get(f"{self.url}/api/v4/files/{fid}/info") as resp:
                    if resp.status != 200:
                        self.logger.warning(
                            "file info %s failed: %s", fid, resp.status,
                        )
                        continue
                    info = await resp.json()
                filename = info.get("name") or fid
                # Strip any path separators the server may have sent
                # so we can't be redirected outside the dest dir.
                filename = os.path.basename(filename) or fid
                dest = dest_root / filename
                async with session.get(f"{self.url}/api/v4/files/{fid}") as resp:
                    if resp.status != 200:
                        self.logger.warning(
                            "file get %s failed: %s", fid, resp.status,
                        )
                        continue
                    data = await resp.read()
                dest.write_bytes(data)
                # Use forward slashes in the reported path so the
                # value looks the same in the user-message preamble
                # across Windows and Linux.
                rel_paths.append(f"attachments/{post_id}/{filename}")
            except Exception as exc:
                self.logger.warning("attachment %s download failed: %s", fid, exc)
        if rel_paths:
            self.logger.info(
                "downloaded %d attachment(s) for post %s -> %s",
                len(rel_paths), post_id, dest_root,
            )
        return rel_paths

    async def listen(self, on_message):
        """
        on_message(channel_id, channel_name, sender_name, sender_email, text, root_id, direct)
            — called for every message in channels the bot is in.
            direct=True when it's a DM or explicit @mention.
            The agent decides whether to reply.
        """
        async with aiohttp.ClientSession(headers=self.headers) as session:
            await self._get_me(session)
            # Resolve team_name -> team_id so delete_team events can
            # be matched. One-time lookup at listen start — the team
            # name from agent.yml is stable, and a later team rename
            # would be surfaced by the delete+create event pair.
            if self.team_name:
                self.team_id = await self.resolve_team_id(session, self.team_name)
            await self._register_as_ai_agent(session)
            # Mark the bot user online immediately so the presence
            # dot lights up within seconds of daemon start. The
            # background heartbeat keeps refreshing it.
            await self._set_online(session)

            # Cancel any background tasks leftover from a previous listen()
            # call (reconnect path) before spawning fresh ones bound to this
            # new session. Each reconnect would otherwise stack up loops
            # pointing at a closed session.
            await self._cancel_background_tasks()
            # Fresh queue + seq per listen — reconnect discards anything
            # queued but not yet pulled (the old session's aiohttp is
            # about to close under us anyway).
            self._queue = asyncio.PriorityQueue()
            self._queue_seq = 0
            self._background_tasks = [
                asyncio.ensure_future(self._ai_agent_heartbeat_loop(session)),
                asyncio.ensure_future(self._status_heartbeat(session)),
                asyncio.ensure_future(self._consume_queue(on_message, session)),
            ]

            try:
                async with session.ws_connect(self.ws_url) as ws:
                    self._ws = ws
                    self._seq = 1
                    await ws.send_json({
                        "seq": self._seq,
                        "action": "authentication_challenge",
                        "data": {"token": self.token},
                    })
                    self.logger.info("WebSocket connected, listening for events...")

                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            event = json.loads(msg.data)
                            await self._handle_event(event, on_message, session)
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            self.logger.warning(
                                "WebSocket %s, reconnecting...",
                                "error" if msg.type == aiohttp.WSMsgType.ERROR else "closed",
                            )
                            break
            finally:
                # Null _ws first so any concurrent background-task await
                # sees None and bails before we close the session under it.
                # Then cancel the background loops so their in-flight
                # requests don't fire against a closed aiohttp session.
                self._ws = None
                await self._cancel_background_tasks()

    async def _cancel_background_tasks(self):
        tasks = self._background_tasks
        self._background_tasks = []
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            # Drain the tasks so any in-flight aiohttp request completes its
            # cancellation before we hand control back to the session's
            # __aexit__ (which will close the underlying connector).
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _handle_event(self, event: dict, on_message, session: aiohttp.ClientSession):
        event_type = event.get("event", "")

        if event_type == "ai_agent_rpc_request":
            await self._handle_rpc_request(event)
            return

        if event_type == "delete_team":
            # Server-side broadcast scoped to team members — we get
            # it when one of the bot's teams (a Puffo "space") was
            # deleted. Hand the team_id to the worker so it can
            # archive itself if the match is its own team.
            data = event.get("data") or {}
            team_id = (data.get("team_id") or "").strip()
            if self._team_deleted_handler and team_id:
                try:
                    await self._team_deleted_handler(team_id)
                except Exception as exc:
                    self.logger.warning(
                        "team_deleted handler error for team %s: %s",
                        team_id, exc,
                    )
            return

        if event_type != "posted":
            return

        post = json.loads(event["data"].get("post", "{}"))
        sender_id = post.get("user_id", "")
        channel_id = post.get("channel_id", "")
        text = post.get("message", "")
        post_id = post.get("id", "")
        # ``raw_root_id`` is empty when the post is NOT in a thread —
        # we need that distinction to decide whether to fetch
        # /posts/{root}/thread vs /channels/{id}/posts. ``root_id``
        # (the fallback to ``post_id``) stays the value we use when
        # POSTing replies, so a reply to a non-threaded post starts
        # a new thread under that post.
        raw_root_id = post.get("root_id", "") or ""
        root_id = raw_root_id or post_id
        # Mattermost create_at is ms-since-epoch. We need this so the
        # consumer can fetch follow-up context "since this message"
        # and so the agent can see the absolute time of each message.
        create_at = int(post.get("create_at", 0) or 0)
        file_ids = post.get("file_ids") or []
        # Mattermost tags automated posts (channel join/leave,
        # header changes, invites) with a type starting with
        # ``system_``. Everything user-authored has ``type=""``.
        post_type = post.get("type", "") or ""

        # Ignore own messages
        if sender_id == self.bot_user_id:
            return

        channel_type = event["data"].get("channel_type", "")
        channel_name = event["data"].get("channel_display_name", channel_id)
        is_dm = channel_type == "D"
        is_mention = f"@{self.bot_username}" in text
        is_system = post_type.startswith("system_")

        user = await self.get_user(sender_id)
        sender_name = user.get("username", sender_id)
        sender_email = user.get("email", "")
        sender_is_bot = bool(user.get("is_bot", False))

        # Pre-download any attached files to the agent's workspace so
        # the agent's Read tool can open them. Relative paths so the
        # same string works on host and in the container.
        attachments = await self._download_attachments(session, post_id, file_ids)

        # Resolve any @-mentions in the message so the agent can
        # distinguish human targets from bot/agent targets and decide
        # whether a reply is needed.
        mentions = await self._resolve_mentions(session, text)

        # Rewrite self-mentions to ``@you(<bot_username>)`` so the
        # agent gets an unambiguous "I'm being addressed" signal in
        # the message text. The wrapped ``(name)`` preserves the
        # agent's own handle for self-references; other @-mentions
        # are left intact so peer handles stay visible.
        clean_text = text.replace(
            f"@{self.bot_username}", f"@you({self.bot_username})",
        ).strip()
        direct = is_dm or is_mention
        priority = _compute_priority(direct, sender_is_bot, is_system)
        attach_summary = f" +{len(attachments)} attachment(s)" if attachments else ""
        self.logger.info(
            f"[prio={priority}] [{'dm' if is_dm else 'mention' if is_mention else 'channel'}] "
            f"[{channel_name}] @{sender_name}:{attach_summary} {clean_text}"
        )

        # Push to the priority queue. The consumer task pulls, fires
        # the view-receipt signal, then dispatches to ``on_message``.
        # The (priority, seq) prefix keeps dicts out of tuple
        # comparison — seq is monotonic so two items never tie past
        # the second element.
        if self._queue is None:
            self.logger.warning(
                "no queue available — dropping post %s (should only happen during shutdown)",
                post_id,
            )
            return
        self._queue_seq += 1
        await self._queue.put((priority, self._queue_seq, {
            "channel_id": channel_id,
            "channel_name": channel_name,
            "sender_name": sender_name,
            "sender_email": sender_email,
            "clean_text": clean_text,
            "root_id": root_id,
            "raw_root_id": raw_root_id,
            "direct": direct,
            "attachments": attachments,
            "sender_is_bot": sender_is_bot,
            "mentions": mentions,
            "post_id": post_id,
            "create_at": create_at,
        }))

    async def _consume_queue(
        self, on_message, session: aiohttp.ClientSession,
    ) -> None:
        """Pull one queued post at a time, fire its view-receipt, and
        dispatch to ``on_message``. Strictly serial — only one turn
        runs per agent at a time — so the claude session's conversation
        history stays coherent and the agent doesn't try to answer two
        channels in parallel (which was causing spurious ``[SILENT]``
        replies when the model got confused by interleaved context).
        """
        queue = self._queue
        assert queue is not None
        while True:
            _priority, _seq, args = await queue.get()
            post_id = args.get("post_id", "")
            # Fire the "viewed by this agent" signal NOW (not at
            # message-receive time) so other users / agents see the
            # icon flip when we actually start processing — a real
            # "paying attention" indicator rather than a delivery
            # receipt.
            await self._post_view_receipt(session, post_id)
            # Fetch any messages that arrived in the same thread or
            # channel since this post — the agent needs the latest
            # state of the conversation, not just what was visible
            # when this post was queued. Without this, a reply to m1
            # could land after the conversation has already moved on
            # via m2, m3, m4.
            followups = await self._fetch_followup_context(
                session,
                channel_id=args["channel_id"],
                raw_root_id=args.get("raw_root_id", ""),
                primary_post_id=post_id,
                since_create_at=args.get("create_at", 0),
            )
            try:
                await on_message(
                    args["channel_id"], args["channel_name"],
                    args["sender_name"], args["sender_email"],
                    args["clean_text"], args["root_id"], args["direct"],
                    args["attachments"], args["sender_is_bot"],
                    args["mentions"],
                    post_id, args.get("create_at", 0), followups,
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.logger.error(
                    f"on_message handler failed for post {post_id}: {e}",
                    exc_info=True,
                )
