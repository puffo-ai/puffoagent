import os

from ._logging import agent_logger
from .adapters import Adapter, TurnContext
from .memory import MemoryManager
from .skills_loader import SkillsLoader
from .usage_tracker import UsageTracker

MAX_LOG_ENTRIES = 60

CATEGORY_ICON = {"soul": "🧬", "skills": "⚡", "memory": "🧠"}
CATEGORY_LABEL = {"soul": "Soul", "skills": "Skills", "memory": "Memory"}


class PuffoAgent:
    def __init__(
        self,
        adapter: Adapter,
        profile_path: str,
        memory_dir: str,
        skills_dirs: list[str] | None = None,
        workspace_dir: str = "",
        claude_dir: str = "",
        agent_id: str = "",
    ):
        """Per-agent shell owned by the portal.

        The shell owns cross-cutting state (conversation log, memory,
        usage, system-prompt assembly) and delegates each turn to an
        ``Adapter``. The adapter owns the agentic loop — see
        ``adapters/base.py``.

        ``skills_dirs`` is a list of directories to merge into the
        skills context: typically ``[daemon_cfg.skills_dir,
        <claude_dir>/skills]`` so agents see both daemon-wide and
        per-agent skills. Earlier entries win on name collision.
        """
        self.adapter = adapter
        self.base_dir = os.path.dirname(os.path.abspath(profile_path))
        self.workspace_dir = workspace_dir
        self.claude_dir = claude_dir
        self.agent_id = agent_id
        self.logger = agent_logger(__name__, agent_id)

        self.memory = MemoryManager(memory_dir)
        self.memory_dir = memory_dir
        self.skills = SkillsLoader(skills_dirs or [])
        self.usage = UsageTracker(memory_dir, agent_id=agent_id)

        with open(profile_path, "r", encoding="utf-8") as f:
            self.profile = f.read()

        # Universal conversation log shared across all channels.
        self.log: list[dict] = []

    # ── Special commands ──────────────────────────────────────────────────────

    def _cmd_profile(self) -> str:
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
        on_progress=None,
    ) -> str | None:
        cmd = text.strip().lower()

        if cmd == "!profile":
            return self._cmd_profile()
        if cmd == "!usage":
            return self._cmd_usage()

        self._append_user(channel_name, sender, sender_email, text)

        ctx = TurnContext(
            system_prompt=self._build_system_prompt(),
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
        skills_ctx = self.skills.get_context()
        if skills_ctx:
            parts.append(skills_ctx)
        return "\n\n".join(parts)
