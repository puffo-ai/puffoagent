import os
import logging
from .memory import MemoryManager
from .skills_loader import SkillsLoader
from .usage_tracker import UsageTracker

logger = logging.getLogger(__name__)

MAX_LOG_ENTRIES = 60

CATEGORY_ICON = {"soul": "🧬", "skills": "⚡", "memory": "🧠"}
CATEGORY_LABEL = {"soul": "Soul", "skills": "Skills", "memory": "Memory"}


class PuffoAgent:
    def __init__(
        self,
        provider,
        profile_path: str,
        memory_dir: str,
        skills_dir: str = "",
    ):
        """Per-agent state owned by the portal.

        The portal passes absolute paths so multiple agents can coexist in
        the same process without stomping on each other. skills_dir is
        optional — an empty string disables the skills loader.
        """
        self.provider = provider
        # base_dir is used by the `!profile` debug command to locate the
        # agent's companion directories (agents/, skills/, memory/).
        self.base_dir = os.path.dirname(os.path.abspath(profile_path))

        self.memory = MemoryManager(memory_dir)
        self.skills = SkillsLoader(skills_dir) if skills_dir else None
        self.usage = UsageTracker(memory_dir)

        with open(profile_path, "r", encoding="utf-8") as f:
            self.profile = f.read()

        # Universal conversation log shared across all channels.
        self.log: list[dict] = []

    # ── Special commands ──────────────────────────────────────────────────────

    def _cmd_profile(self) -> str:
        """Return all .md files formatted as markdown."""
        dirs = [
            ("soul",   os.path.join(self.base_dir, "agents")),
            ("skills", os.path.join(self.base_dir, "skills")),
            ("memory", os.path.join(self.base_dir, "memory")),
        ]
        parts = ["## Agent Profile\n"]
        for category, directory in dirs:
            if not os.path.isdir(directory):
                continue
            files = [f for f in sorted(os.listdir(directory)) if f.endswith(".md") and f != "README.md"]
            if not files:
                continue
            icon = CATEGORY_ICON.get(category, "📄")
            label = CATEGORY_LABEL.get(category, category.title())
            parts.append(f"### {icon} {label}")
            for fname in files:
                fpath = os.path.join(directory, fname)
                with open(fpath, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                parts.append(f"**`{fname}`**\n```\n{content}\n```")
        return "\n\n".join(parts)

    def _cmd_usage(self) -> str:
        """Return token usage stats formatted as markdown."""
        stats = self.usage.stats()
        at = stats["all_time"]
        lines = [
            "## Token Usage\n",
            f"**All-time:** {at['total']:,} tokens "
            f"({at['input']:,} input · {at['output']:,} output) "
            f"over {at['calls']:,} calls\n",
        ]
        for granularity in ("daily", "weekly", "monthly", "hourly"):
            periods = stats[granularity][-10:]  # last 10 periods
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

    def handle_message(
        self, channel_id: str, channel_name: str, sender: str, sender_email: str, text: str, direct: bool = False
    ) -> str | None:
        cmd = text.strip().lower()

        # Special commands — bypass the LLM entirely
        if cmd == "!profile":
            return self._cmd_profile()
        if cmd == "!usage":
            return self._cmd_usage()

        self._append_user(channel_name, sender, sender_email, text)

        system_prompt = self._build_system_prompt()
        reply, input_tokens, output_tokens = self.provider.complete(system_prompt, list(self.log))
        self.usage.record(input_tokens, output_tokens)

        if reply.strip() == "[SILENT]":
            logger.debug(f"[silent] [{channel_name}] @{sender}: agent chose not to reply")
            return None

        self._append_assistant(channel_name, reply)
        return reply

    def _append_user(self, channel_name: str, sender: str, sender_email: str, text: str):
        # Structured markdown block makes it obvious to the LLM what is
        # context metadata and what is the actual message content, which
        # avoids the LLM echoing "[#channel] @user:" style prefixes into
        # its replies.
        lines = [
            "- channel: " + channel_name,
            f"- sender: {sender}" + (f" ({sender_email})" if sender_email else ""),
            "- message: " + text,
        ]
        self.log.append({"role": "user", "content": "\n".join(lines)})
        self._truncate_log()

    def _append_assistant(self, channel_name: str, reply: str):
        # Store the assistant reply as-is, no channel/sender prefix.
        self.log.append({"role": "assistant", "content": reply})
        self._truncate_log()

    def _truncate_log(self):
        if len(self.log) > MAX_LOG_ENTRIES:
            self.log = self.log[-MAX_LOG_ENTRIES:]

    def _build_system_prompt(self) -> str:
        parts = [self.profile]
        memory_ctx = self.memory.get_context()
        if memory_ctx:
            parts.append(memory_ctx)
        if self.skills is not None:
            skills_ctx = self.skills.get_context()
            if skills_ctx:
                parts.append(skills_ctx)
        return "\n\n".join(parts)
