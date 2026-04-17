import asyncio
import json
import os
from pathlib import Path
import aiohttp

from ._logging import agent_logger

# How often to refresh the online status (seconds)
STATUS_HEARTBEAT_INTERVAL = 30
# How often the puffoagent-specific heartbeat is sent to /aiagents/me/heartbeat.
AI_AGENT_HEARTBEAT_INTERVAL = 30
# How often buffered "I observed this post" ids are flushed to the server.
VIEW_RECEIPT_FLUSH_INTERVAL = 5


class MattermostClient:
    def __init__(
        self,
        url: str,
        token: str,
        profile_name: str = "default",
        file_server_url: str = "",
        agent_id: str = "",
        workspace_dir: str = "",
    ):
        self.url = url.rstrip("/")
        self.token = token
        self.profile_name = profile_name
        self.file_server_url = file_server_url
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
        self._view_buffer: set[str] = set()
        self._view_lock = asyncio.Lock()
        self._rpc_handler = None  # set by the agent to serve file RPC calls
        self._background_tasks: list[asyncio.Task] = []
        self.logger = agent_logger(__name__, agent_id)

    async def _get_me(self, session: aiohttp.ClientSession):
        async with session.get(f"{self.url}/api/v4/users/me") as resp:
            data = await resp.json()
            self.bot_user_id = data["id"]
            self.bot_username = data["username"]
            self.logger.info(f"Logged in as @{self.bot_username} ({self.bot_user_id})")

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

    async def record_post_viewed(self, post_id: str):
        """Buffer a post id for the next batch flush."""
        if not post_id:
            return
        async with self._view_lock:
            self._view_buffer.add(post_id)

    async def _view_flush_loop(self, session: aiohttp.ClientSession):
        """Flush buffered post view receipts every VIEW_RECEIPT_FLUSH_INTERVAL.
        Uses POST /posts/views/batch; server broadcasts an
        ai_agent_post_viewed WS event per post so the webapp updates the
        viewed-by icon without a page reload.
        """
        while True:
            await asyncio.sleep(VIEW_RECEIPT_FLUSH_INTERVAL)
            async with self._view_lock:
                if not self._view_buffer:
                    continue
                batch = list(self._view_buffer)
                self._view_buffer.clear()
            try:
                async with session.post(
                    f"{self.url}/api/v4/posts/views/batch",
                    json={"post_ids": batch},
                ) as resp:
                    if resp.status not in (200, 201):
                        body = await resp.text()
                        self.logger.warning(f"View batch flush failed: {resp.status} {body}")
                        # Re-buffer on failure so we don't drop view receipts.
                        async with self._view_lock:
                            self._view_buffer.update(batch)
            except Exception as e:
                self.logger.warning(f"View batch flush error: {e}")
                async with self._view_lock:
                    self._view_buffer.update(batch)

    def set_rpc_handler(self, handler):
        """Register a callable that services `ai_agent_rpc_request` events.

        handler(cmd: str, args: dict) -> (ok: bool, data: dict, error: str|None)
        """
        self._rpc_handler = handler

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
        if self._ws is None:
            return
        self._seq += 1
        await self._ws.send_json({
            "seq": self._seq,
            "action": "ai_agent_rpc_response",
            "data": {
                "request_id": request_id,
                "ok": ok,
                "data": data or {},
                "error": error or "",
            },
        })

    async def _set_online(self):
        """Set bot status to online via REST API."""
        async with aiohttp.ClientSession(headers=self.headers) as session:
            payload = {"user_id": self.bot_user_id, "status": "online"}
            async with session.put(f"{self.url}/api/v4/users/{self.bot_user_id}/status", json=payload) as resp:
                if resp.status not in (200, 201):
                    self.logger.warning(f"Failed to set online status: {resp.status}")

    async def _status_heartbeat(self):
        """Keep the bot status as online by refreshing periodically."""
        while True:
            try:
                await self._set_online()
            except Exception as e:
                self.logger.warning(f"Status heartbeat error: {e}")
            await asyncio.sleep(STATUS_HEARTBEAT_INTERVAL)

    async def send_typing(self, channel_id: str, parent_id: str = ""):
        """Send a typing indicator for this channel via WebSocket."""
        if self._ws is None:
            return
        try:
            self._seq += 1
            await self._ws.send_json({
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
            await self._register_as_ai_agent(session)
            await self._set_online()

            # Cancel any background tasks leftover from a previous listen()
            # call (reconnect path) before spawning fresh ones bound to this
            # new session. Each reconnect would otherwise stack up loops
            # pointing at a closed session.
            await self._cancel_background_tasks()
            self._background_tasks = [
                asyncio.ensure_future(self._status_heartbeat()),
                asyncio.ensure_future(self._ai_agent_heartbeat_loop(session)),
                asyncio.ensure_future(self._view_flush_loop(session)),
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
                            self.logger.warning("WebSocket closed/error, reconnecting...")
                            break
            finally:
                # Always kill the background loops before letting the session
                # context manager close the aiohttp.ClientSession — otherwise
                # their in-flight requests fire against a closed session.
                await self._cancel_background_tasks()
                self._ws = None

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

        if event_type != "posted":
            return

        post = json.loads(event["data"].get("post", "{}"))
        sender_id = post.get("user_id", "")
        channel_id = post.get("channel_id", "")
        text = post.get("message", "")
        post_id = post.get("id", "")
        root_id = post.get("root_id", "") or post_id
        file_ids = post.get("file_ids") or []

        # Ignore own messages
        if sender_id == self.bot_user_id:
            return

        # Every observed post (including ones we don't reply to) should
        # generate a view receipt.
        await self.record_post_viewed(post_id)

        channel_type = event["data"].get("channel_type", "")
        channel_name = event["data"].get("channel_display_name", channel_id)
        is_dm = channel_type == "D"
        is_mention = f"@{self.bot_username}" in text

        user = await self.get_user(sender_id)
        sender_name = user.get("username", sender_id)
        sender_email = user.get("email", "")

        # Pre-download any attached files to the agent's workspace so
        # the agent's Read tool can open them. Relative paths so the
        # same string works on host and in the container.
        attachments = await self._download_attachments(session, post_id, file_ids)

        clean_text = text.replace(f"@{self.bot_username}", "").strip()
        attach_summary = f" +{len(attachments)} attachment(s)" if attachments else ""
        self.logger.info(
            f"[{'dm' if is_dm else 'mention' if is_mention else 'channel'}] "
            f"[{channel_name}] @{sender_name}:{attach_summary} {clean_text}"
        )
        await on_message(
            channel_id, channel_name, sender_name, sender_email,
            clean_text, root_id, is_dm or is_mention, attachments,
        )
