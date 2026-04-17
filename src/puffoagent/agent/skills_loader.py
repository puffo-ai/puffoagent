"""Load skill markdown files from one or more directories.

Skills layer into the system prompt for adapters that don't have a
native skills concept (today: chat-only). Runtime adapters that have
their own skills discovery — SDK via ``setting_sources=["project"]``
and the claude CLI natively via ``<cwd>/.claude/skills/`` — see these
files directly through the on-disk convention and do not need this
loader at all.
"""

import os
import glob


class SkillsLoader:
    def __init__(self, skill_dirs: list[str] | str):
        # Accept a single string for backwards compatibility with old
        # callers; normalise to a list.
        if isinstance(skill_dirs, str):
            skill_dirs = [skill_dirs] if skill_dirs else []
        self.skill_dirs = [d for d in skill_dirs if d]
        self.skills: list[dict] = []
        self._load()

    def _load(self):
        self.skills = []
        seen_names: set[str] = set()
        # Walk in order so earlier dirs win on name collision; this
        # lets per-agent skills shadow daemon-wide ones when a user
        # wants agent-specific behaviour.
        for skills_dir in self.skill_dirs:
            if not os.path.isdir(skills_dir):
                continue
            for path in sorted(glob.glob(os.path.join(skills_dir, "*.md"))):
                name = os.path.basename(path)
                if name == "README.md" or name in seen_names:
                    continue
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
                self.skills.append({"file": name, "content": content})
                seen_names.add(name)

    def get_context(self) -> str:
        if not self.skills:
            return ""
        parts = ["## Available Skills\n"]
        for skill in self.skills:
            parts.append(skill["content"])
        return "\n".join(parts)
