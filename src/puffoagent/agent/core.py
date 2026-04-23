import os

from ._logging import agent_logger
from ._time import ms_to_iso as _ms_to_iso
from .adapters import Adapter, TurnContext
from .memory import MemoryManager

MAX_LOG_ENTRIES = 60


class PuffoAgent:
    def __init__(
        self,
        adapter: Adapter,
        system_prompt: str,
        memory_dir: str,
        workspace_dir: str = "",
        claude_dir: str = "",
        agent_id: str = "",
    ):
        """Per-agent shell owned by the portal.

        The shell owns cross-cutting state (conversation log, usage,
        memory manager) and delegates each turn to an ``Adapter``.
        The adapter owns the agentic loop — see ``adapters/base.py``.

        ``system_prompt`` is pre-assembled by the worker from the
        shared puffo primer, the agent's ``profile.md``, and a
        snapshot of the memory directory. It is the same content
        written to ``<workspace>/.claude/CLAUDE.md`` so CLI runtimes
        discover it via Claude Code's project-level file lookup while
        sdk-local/chat-local see it here as a string.
        """
        self.adapter = adapter
        self.system_prompt = system_prompt
        self.workspace_dir = workspace_dir
        self.claude_dir = claude_dir
        self.agent_id = agent_id
        self.logger = agent_logger(__name__, agent_id)

        self.memory = MemoryManager(memory_dir)
        self.memory_dir = memory_dir

        # Universal conversation log shared across all channels.
        self.log: list[dict] = []

    # ── Message handling ──────────────────────────────────────────────────────

    async def handle_message(
        self,
        channel_id: str,
        channel_name: str,
        sender: str,
        sender_email: str,
        text: str,
        direct: bool = False,
        attachments: list[str] | None = None,
        sender_is_bot: bool = False,
        mentions: list[dict] | None = None,
        on_progress=None,
        post_id: str = "",
        root_id: str = "",
        create_at: int = 0,
        followups: list[dict] | None = None,
    ) -> str | None:
        self._append_user(
            channel_name, sender, sender_email, text,
            channel_id=channel_id,
            root_id=root_id,
            attachments=attachments,
            sender_is_bot=sender_is_bot,
            mentions=mentions,
            post_id=post_id,
            create_at=create_at,
            followups=followups,
        )

        ctx = TurnContext(
            system_prompt=self.system_prompt,
            messages=list(self.log),
            workspace_dir=self.workspace_dir,
            claude_dir=self.claude_dir,
            memory_dir=self.memory_dir,
            on_progress=on_progress,
        )
        result = await self.adapter.run_turn(ctx)

        # Substring match (not equality): the primer asks for exactly
        # ``[SILENT]`` but agents sometimes hedge with surrounding
        # prose (e.g. "[SILENT] I wasn't mentioned"). Any reply
        # containing the token is treated as silent.
        if not result.reply or "[SILENT]" in result.reply:
            self.logger.debug(f"[silent] [{channel_name}] @{sender}: agent chose not to reply")
            return None

        # Double-post guard. The worker posts the shell's auto-reply
        # to ``(channel_id, root_id)`` — the same slot as the incoming
        # message. If the agent ALSO called ``send_message`` this turn
        # targeting that same slot, MCP already posted there on the
        # agent's behalf; the narration text in result.reply would
        # land as a duplicate. We still append to ``agent.log`` so the
        # next turn sees the narration as context — just skip the
        # outbound post.
        if self._send_message_covered_current_slot(
            targets=result.metadata.get("send_message_targets", []),
            channel_id=channel_id,
            channel_name=channel_name,
            root_id=root_id,
        ):
            self.logger.debug(
                f"[suppress] [{channel_name}] @{sender}: send_message "
                f"already posted to (channel={channel_id or channel_name}, "
                f"root_id={root_id!r}); skipping auto-reply"
            )
            self._append_assistant(channel_name, result.reply)
            return None

        self._append_assistant(channel_name, result.reply)
        return result.reply

    @staticmethod
    def _send_message_covered_current_slot(
        targets: list[dict],
        channel_id: str,
        channel_name: str,
        root_id: str,
    ) -> bool:
        """True iff at least one ``send_message`` call in this turn
        posted to the same ``(channel, thread)`` the shell's auto-
        reply would use.

        Matching rules:

          * **Channel match.** ``send_message`` accepts either a
            channel ID or a channel name, so the tool's ``channel``
            arg must equal the incoming message's ``channel_id`` OR
            ``channel_name``. DMs addressed via ``@handle`` form
            won't match the internal ``user1__user2`` channel name
            — a known limitation, acceptable for now since agents
            typically DM via channel_id.

          * **Thread match.** Both strings must be equal. Empty
            string means "top-level post in the channel"; a non-empty
            value is a specific thread root. Equal non-empty strings
            mean the same thread; equal empty strings mean both post
            top-level in the same channel (still a user-visible
            duplicate). Different values (one top-level, one threaded,
            or two different threads) are distinct conversations and
            do NOT trigger suppression.
        """
        for target in targets:
            t_channel = target.get("channel", "")
            if not t_channel:
                continue
            if t_channel != channel_id and t_channel != channel_name:
                continue
            if target.get("root_id", "") != root_id:
                continue
            return True
        return False

    def _append_user(
        self,
        channel_name: str,
        sender: str,
        sender_email: str,
        text: str,
        attachments: list[str] | None,
        channel_id: str = "",
        root_id: str = "",
        sender_is_bot: bool = False,
        mentions: list[dict] | None = None,
        post_id: str = "",
        create_at: int = 0,
        followups: list[dict] | None = None,
    ):
        # Structured markdown block makes it obvious to the LLM what is
        # context metadata and what is the actual message content, which
        # avoids the LLM echoing "[#channel] @user:" style prefixes into
        # its replies. Matches the format documented in the shared
        # puffo primer (see shared_content.DEFAULT_SHARED_CLAUDE_MD).
        lines = [
            "- channel: " + channel_name,
        ]
        if channel_id:
            lines.append(f"- channel_id: {channel_id}")
        if post_id:
            lines.append(f"- post_id: {post_id}")
        # thread_root_id is the post id to pass as send_message's root_id
        # when replying in this thread. For a top-level post the root is
        # the post itself, so we surface post_id either way — the agent
        # never has to think about which to use.
        thread_root = root_id or post_id
        if thread_root:
            lines.append(f"- thread_root_id: {thread_root}")
        ts_iso = _ms_to_iso(create_at)
        if ts_iso:
            lines.append(f"- timestamp: {ts_iso}")
        lines.append(
            f"- sender: {sender}" + (f" ({sender_email})" if sender_email else "")
        )
        lines.append(f"- sender_type: {'bot' if sender_is_bot else 'human'}")
        if mentions:
            lines.append("- mentions:")
            for m in mentions:
                kind = "bot" if m.get("is_bot") else "human"
                # The self marker pairs with the @you(name) rewrite
                # in the message body — two independent signals so
                # agents that only parse one layer still spot it.
                marker = " — that's you" if m.get("is_self") else ""
                lines.append(f"  - {m['username']} ({kind}){marker}")
        if attachments:
            lines.append("- attachments:")
            for path in attachments:
                lines.append(f"  - {path}")
        lines.append("- message: " + text)
        if followups:
            # Messages that arrived in the same thread / channel
            # AFTER this one was queued. The agent should read them
            # before committing to a reply — the conversation may
            # have moved on, made this question redundant, or
            # answered itself. The agent should only respond if its
            # reply still adds value given everything below.
            lines.append("- followup_messages_since:")
            for f in followups:
                ts = f.get("timestamp", "") or _ms_to_iso(f.get("create_at", 0))
                fid = f.get("id", "")
                fsender = f.get("sender_username", "") or f.get("sender_id", "")
                ftext = f.get("text", "") or ""
                lines.append(
                    f"  - [{ts} post:{fid}] @{fsender}: {ftext}"
                )
        self.log.append({"role": "user", "content": "\n".join(lines)})
        self._truncate_log()

    def _append_assistant(self, channel_name: str, reply: str):
        self.log.append({"role": "assistant", "content": reply})
        self._truncate_log()

    def _truncate_log(self):
        if len(self.log) > MAX_LOG_ENTRIES:
            self.log = self.log[-MAX_LOG_ENTRIES:]
