"""Shared content + CLAUDE.md assembly.

Every agent, regardless of runtime, needs the same baseline
understanding of the Puffo.ai platform (how messages arrive, what
channels are, how to stay silent, where attachments land). That
content lives in one place — ``~/.puffoagent/docker/shared/CLAUDE.md``
— and gets folded into each agent's generated
``<workspace>/.claude/CLAUDE.md`` at worker startup.

``ensure_shared_primer`` bootstraps the file on first use with a
built-in default so a fresh install has something useful; users can
edit it freely afterwards.

``assemble_claude_md`` produces the per-agent CLAUDE.md from three
ingredients: the shared primer, the agent's ``profile.md`` soul, and
a snapshot of the agent's memory directory. This file becomes the
authoritative system-prompt source for every runtime — SDK/chat-only
read it and prepend to system_prompt; cli-local/cli-docker let Claude
Code auto-discover it via ``<cwd>/.claude/CLAUDE.md``.
"""

from __future__ import annotations

import os
from pathlib import Path


DEFAULT_SHARED_CLAUDE_MD = """\
# Puffo.ai platform primer

You are an AI agent running on the [Puffo.ai](https://puffo.ai)
platform, hosted by the `puffoagent` daemon on a human operator's
machine. This primer is shared across every agent the operator runs;
your specific role lives in the *Your role* section below.

## How messages arrive

Every user message is wrapped in a metadata block:

```
- channel: <channel name>
- sender: <username> (<email>)
- attachments:                 # only present when files are attached
  - attachments/<post_id>/<filename>
  - ...
- message: <actual message text>
```

Reply only to the `message:` field's content. Never echo the metadata
block, field labels (`message:`), or bracketed prefixes (`[#channel]`)
in your response. Address users with `@username` inline when needed.

## Channels, DMs, teams

- **Direct message (DM):** one-on-one. The `channel` field starts
  with `@`. Your reply goes only to the other participant.
- **Public / private channel:** a multi-user room. Weigh whether
  your response is relevant to everyone present before sending.
- **Team:** channels are grouped under teams. You only see channels
  in teams whose `bot` account you've been added to.

## When to stay silent

If the conversation is between other people and your response isn't
needed, output exactly `[SILENT]` (six characters, nothing else).
The shell will drop it — nothing is posted.

## Attachments

When a message arrives with attached files, they are auto-downloaded
to `attachments/<post_id>/<filename>` relative to your current working
directory (your workspace). The metadata block's `attachments:` list
gives you the exact relative paths; use your `Read` tool to open
them. Don't try to fetch files yourself — the shell has already
done that work.

## Your workspace

Your `cwd` is `/workspace` (inside a container) or
`~/.puffoagent/agents/<your-id>/workspace/` (on the host). This
directory survives daemon restarts and, for cli-docker, container
restarts. Anything outside it may be ephemeral.

## Memory

A snapshot of your memory is included in this CLAUDE.md. If you need
to remember something across sessions, write it as markdown into the
`memory/` directory under your agent root. Memory updates take
effect on the next worker restart (pause/resume the agent to force).
"""


DEFAULT_SHARED_README = """\
# Shared context for all cli-docker / cli-local agents

Files in this directory are inlined into every agent's generated
`workspace/.claude/CLAUDE.md` on worker startup. Edit to customise
the primer your bots see.

Primary file: `CLAUDE.md` — the baseline platform primer. Safe to
edit; your changes apply to all agents on the next worker restart.

If you add other markdown files here, they are not auto-included
today; extend `ensure_shared_primer` or the `assemble_claude_md`
helper to pick them up.
"""


def ensure_shared_primer(shared_dir: Path) -> None:
    """Create ``shared_dir`` and seed it with a default primer on
    first use. Idempotent — never overwrites existing files.
    """
    shared_dir.mkdir(parents=True, exist_ok=True)
    primer = shared_dir / "CLAUDE.md"
    if not primer.exists():
        primer.write_text(DEFAULT_SHARED_CLAUDE_MD, encoding="utf-8")
    readme = shared_dir / "README.md"
    if not readme.exists():
        readme.write_text(DEFAULT_SHARED_README, encoding="utf-8")


def read_shared_primer(shared_dir: Path) -> str:
    """Return the shared CLAUDE.md contents, or empty string if
    absent. Callers should invoke ``ensure_shared_primer`` first."""
    path = shared_dir / "CLAUDE.md"
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def read_memory_snapshot(memory_dir: Path) -> str:
    """Concatenate every ``*.md`` file in ``memory_dir`` into one
    block. Files are sorted so output is deterministic across
    restarts. Returns empty string if the directory is missing or
    empty.
    """
    if not memory_dir.is_dir():
        return ""
    parts: list[str] = []
    for path in sorted(memory_dir.glob("*.md")):
        if path.name == "README.md":
            continue
        try:
            body = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if not body:
            continue
        parts.append(f"### {path.stem}\n\n{body}")
    return "\n\n".join(parts)


def assemble_claude_md(
    *,
    shared_primer: str,
    profile: str,
    memory_snapshot: str,
) -> str:
    """Produce the per-agent CLAUDE.md content. Order matters:
    shared primer first (platform conventions), then the agent's
    role, then memory. Claude reads top-down, and a well-defined
    role benefits from knowing the platform first.
    """
    parts: list[str] = []
    if shared_primer.strip():
        parts.append(shared_primer.strip())
    if profile.strip():
        parts.append("---\n\n# Your role\n\n" + profile.strip())
    if memory_snapshot.strip():
        parts.append("---\n\n# Your memory\n\n" + memory_snapshot.strip())
    return "\n\n".join(parts) + "\n"


def write_claude_md(workspace_dir: Path, content: str) -> Path:
    """Write ``content`` to ``<workspace>/.claude/CLAUDE.md``. Makes
    the target directory if needed. Returns the written path.
    """
    claude_dir = workspace_dir / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    path = claude_dir / "CLAUDE.md"
    path.write_text(content, encoding="utf-8")
    return path
