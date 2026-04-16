import os
import glob


class SkillsLoader:
    def __init__(self, skills_dir: str):
        self.skills_dir = skills_dir
        self.skills: list[dict] = []
        self._load()

    def _load(self):
        self.skills = []
        for path in glob.glob(os.path.join(self.skills_dir, "*.md")):
            if os.path.basename(path) == "README.md":
                continue
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            self.skills.append({"file": os.path.basename(path), "content": content})

    def get_context(self) -> str:
        if not self.skills:
            return ""
        parts = ["## Available Skills\n"]
        for skill in self.skills:
            parts.append(skill["content"])
        return "\n".join(parts)
