# Skills

This directory stores the agent's skills as markdown files.
Each file describes a capability the agent can invoke.

New skills can be added by dropping a `.md` file here — the agent picks them up automatically.

## Format

```markdown
---
name: <skill name>
trigger: <keyword or phrase that activates this skill>
---

## Description
What this skill does.

## Instructions
Step-by-step instructions the agent follows when this skill is triggered.
```
