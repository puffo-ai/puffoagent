import os

from ._logging import agent_logger
from .adapters import Adapter, TurnContext
from .memory import MemoryManager
from .usage_tracker import UsageTracker

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
        self.usage = UsageTracker(memory_dir, agent_id=agent_id)

        # Universal conversation log shared across all channels.
        self.log: list[dict] = []

    # ── Special commands ──────────────────────────────────────────────────────

    def _cmd_usage(self) -> str:
        stats = self.usage.stats()
        at = stats["all_time"]
        lines = [
            "## Token Usage\n",
            f"**All-time:** {at['total']:,} tokens "
            f"({at['input']:,} input · {at['output']:,} output) "
            f"over {at['calls']:,} calls\n",
        ]
        for granularity in ("daily", "weekly", "monthly", "hourly"):
            periods = stats[granularity][-10:]
            if not periods:
                continue
            label = granularity.title()
            lines.append(f"### {label} (last {len(periods)})")
            lines.append("| Period | Input | Output | Total |")
            lines.append("|--------|------:|-------:|------:|")
            for p in periods:
                lines.append(f"| {p['period']} | {p['input']:,} | {p['output']:,} | {p['total']:,} |")
            lines.append("")
        return "\n".join(lines)

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
        on_progress=None,
    ) -> str | None:
        if text.strip().lower() == "!usage":
            return self._cmd_usage()

        self._append_user(channel_name, sender, sender_email, text, attachments)

        ctx = TurnContext(
            system_prompt=self.system_prompt,
            messages=list(self.log),
            workspace_dir=self.workspace_dir,
            claude_dir=self.claude_dir,
            memory_dir=self.memory_dir,
            on_progress=on_progress,
        )
        result = await self.adapter.run_turn(ctx)
        self.usage.record(result.input_tokens, result.output_tokens)

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
    ):
        # Structured markdown block makes it obvious to the LLM what is
        # context metadata and what is the actual message content, which
        # avoids the LLM echoing "[#channel] @user:" style prefixes into
        # its replies. Matches the format documented in the shared
        # puffo primer (see shared_content.DEFAULT_SHARED_CLAUDE_MD).
        lines = [
            "- channel: " + channel_name,
            f"- sender: {sender}" + (f" ({sender_email})" if sender_email else ""),
        ]
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
