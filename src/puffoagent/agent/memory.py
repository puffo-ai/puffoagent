import os
import glob
from datetime import datetime


class MemoryManager:
    def __init__(self, memory_dir: str):
        self.memory_dir = memory_dir
        self.memories: dict[str, str] = {}
        self._load()

    def _load(self):
        for path in glob.glob(os.path.join(self.memory_dir, "*.md")):
            if os.path.basename(path) == "README.md":
                continue
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            topic = os.path.splitext(os.path.basename(path))[0]
            self.memories[topic] = content

    def get_context(self) -> str:
        if not self.memories:
            return ""
        parts = ["## Memory\n"]
        for topic, content in self.memories.items():
            parts.append(f"### {topic}\n{content}\n")
        return "\n".join(parts)

    def save(self, topic: str, content: str):
        self.memories[topic] = content
        safe_topic = topic.replace(" ", "_").replace("/", "-")
        path = os.path.join(self.memory_dir, f"{safe_topic}.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"---\ntopic: {topic}\nupdated: {datetime.utcnow().isoformat()}\n---\n\n{content}\n")
