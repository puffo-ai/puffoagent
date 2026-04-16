# Memory

This directory stores the agent's persistent memory as markdown files.
Each file represents a topic or a user the agent has learned about.

Files are loaded at startup and updated automatically during conversations.

## Format

```markdown
---
topic: <topic name>
updated: <ISO date>
---

<memory content>
```
