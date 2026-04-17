import os

from ._logging import agent_logger
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
        SDK/chat-only see it here as a string.
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
    ) -> str | None:
        self._append_user(
            channel_name, sender, sender_email, text,
            attachments=attachments,
            sender_is_bot=sender_is_bot,
            mentions=mentions,
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

        if not result.reply or result.reply.strip() == "[SILENT]":
            self.logger.debug(f"[silent] [{channel_name}] @{sender}: agent chose not to reply")
            return None

        self._append_assistant(channel_name, result.reply)
        return result.reply

    def _append_user(
        self,
        channel_name: str,
        sender: str,
        sender_email: str,
        text: str,
        attachments: list[str] | None,
        sender_is_bot: bool = False,
        mentions: list[dict] | None = None,
    ):
        # Structured markdown block makes it obvious to the LLM what is
        # context metadata and what is the actual message content, which
        # avoids the LLM echoing "[#channel] @user:" style prefixes into
        # its replies. Matches the format documented in the shared
        # puffo primer (see shared_content.DEFAULT_SHARED_CLAUDE_MD).
        lines = [
            "- channel: " + channel_name,
            f"- sender: {sender}" + (f" ({sender_email})" if sender_email else ""),
            f"- sender_type: {'bot' if sender_is_bot else 'human'}",
        ]
        if mentions:
            lines.append("- mentions:")
            for m in mentions:
                kind = "bot" if m.get("is_bot") else "human"
                lines.append(f"  - {m['username']} ({kind})")
        if attachments:
            lines.append("- attachments:")
            for path in attachments:
                lines.append(f"  - {path}")
        lines.append("- message: " + text)
        self.log.append({"role": "user", "content": "\n".join(lines)})
        self._truncate_log()

    def _append_assistant(self, channel_name: str, reply: str):
        self.log.append({"role": "assistant", "content": reply})
        self._truncate_log()

    def _truncate_log(self):
        if len(self.log) > MAX_LOG_ENTRIES:
            self.log = self.log[-MAX_LOG_ENTRIES:]
