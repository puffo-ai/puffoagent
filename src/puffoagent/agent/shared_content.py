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

## Proactive actions via the `puffo` MCP tools

Your reply is posted automatically to the channel the message came
from. If you also need to post to a **different** channel, DM
another user, or upload a file, use the `puffo` MCP tools:

- `mcp__puffo__send_message(channel, text, root_id="")`
  - `channel`: `"@username"` for a DM, `"#channel-name"` for a named
    channel in your team, or a raw 26-char channel id.
  - Returns a confirmation with the new post id.

- `mcp__puffo__upload_file(path, channel, caption="")`
  - `path` is relative to your workspace. Don't try to read `/etc/passwd`
    — the tool refuses anything outside the workspace.

- `mcp__puffo__list_channels()` lists the channels you have access to,
  showing id, type (D for DM, O for public, P for private), and name.

Use these sparingly and with intent — messages you post proactively
will surprise people. If a user explicitly asked you to notify
someone, go ahead; if they didn't, ask first.

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

## Permission prompts (cli-local only)

If you are running in `cli-local` mode, any tool invocation that
isn't pre-approved goes through a permission prompt that is posted
to your human owner's DM. The owner replies `y` / `n` within a few
minutes; if they don't, the request is denied and you'll see a
`permission request timed out` error. Plan for this latency — don't
chain many permission-requiring tool calls if the user seems
inattentive.
"""


DEFAULT_SHARED_README = """\
# Shared context for all puffoagent agents

Files in this directory are folded into every agent on worker
startup:

- `CLAUDE.md` — the baseline platform primer, inlined into each
  agent's generated `workspace/.claude/CLAUDE.md`.
- `skills/*.md` — copied into each agent's
  `workspace/.claude/skills/`, where Claude Code and the SDK
  adapter pick them up as in-context capability descriptions.

Edit freely; changes apply on the next worker restart (pause/resume
an agent to force).
"""


# ── Default skill markdowns ───────────────────────────────────────────────────


DEFAULT_SKILL_SEND_MESSAGE = """\
# Skill: send_message

Post a message to a Puffo.ai channel or DM a user.

**Tool:** `mcp__puffo__send_message`

**Arguments:**
- `channel` (required) — one of:
  - `"@username"` to DM a user
  - `"#channel-name"` to post in a named channel in your team
  - a raw 26-char channel id
- `text` (required) — message body; Markdown is supported
- `root_id` (optional) — post id to reply inside an existing thread

**When to use:**
- The user asked you to notify someone who is not in the current
  conversation ("let the team know…", "ping Alice about…").
- You are reporting to a specific status / standup channel the user
  explicitly designated.

**When NOT to use:**
- Your ordinary reply to the incoming message — that's auto-posted
  to the originating channel; calling send_message on top of it would
  cause duplicates.
- Spontaneous cross-posting that wasn't requested.

**Example:**

```
send_message(channel="@alice", text="Heads up — your build finished.")
send_message(channel="#eng-standup", text="Daily: shipped X, in progress Y.")
```
"""


DEFAULT_SKILL_UPLOAD_FILE = """\
# Skill: upload_file

Upload a file from your workspace to a Puffo.ai channel.

**Tool:** `mcp__puffo__upload_file`

**Arguments:**
- `path` (required) — workspace-relative file path, e.g.
  `reports/weekly.pdf`. Absolute paths that escape the workspace
  are refused.
- `channel` (required) — same syntax as `send_message`.
- `caption` (optional) — text posted alongside the file.

**Workflow:** generate or fetch a file into your workspace (Write
tool / Bash / attachments), then call `upload_file` to attach it to
a Puffo.ai post.

**Example:**

```
# 1. Write the report
Write(file_path="weekly.md", content="# Week of …")
# 2. Upload it
upload_file(path="weekly.md", channel="#eng-standup", caption="weekly report")
```
"""


DEFAULT_SKILL_ATTACHMENTS = """\
# Skill: attachments

Files attached to incoming Mattermost messages are auto-downloaded
to your workspace before each turn. You never need to fetch them
yourself.

**Where they land:** `attachments/<post_id>/<filename>` relative to
your workspace root.

**How you're told:** the user-message preamble includes an
`attachments:` list with the relative paths:

```
- channel: @alice
- sender: alice (alice@example.com)
- attachments:
  - attachments/p_abc/spec.pdf
  - attachments/p_abc/screenshot.png
- message: please review these and tell me what's wrong
```

**What to do:** use your `Read` tool on the listed paths.

**Note:** attachments persist across turns but a file with the same
post id can be re-downloaded if the user posts a new version — don't
assume the path is immutable across conversations.
"""


DEFAULT_SKILL_PERMISSIONS = """\
# Skill: permission prompts (cli-local only)

If you are running in `cli-local` mode, any tool invocation your
operator hasn't pre-approved is routed to them via Mattermost DM for
approval.

**What the operator sees:** a DM that looks like

```
🔐 agent `<your-id>` wants to run `Bash`
- command: `git push origin main`
reply `y` to approve, `n` to deny (times out in 300s)
```

**What you see:**
- On approve: the tool runs normally and you get its output.
- On deny: a tool error with `owner denied the request`.
- On timeout: a tool error with `permission request timed out`.

**Guidance:**
- Batch permission-sensitive work thoughtfully — each request pings
  the operator. Plan the whole change, then ask once.
- Explain what you're doing in your reply *before* making the call,
  so the DM the operator receives has context from your previous
  message.
- If the operator denies or times out repeatedly, stop retrying and
  ask them directly whether the task is still wanted.

This skill does not apply to `sdk` or `cli-docker` runtimes: SDK
agents use an allowlist, and cli-docker agents run in a sandboxed
container with `--dangerously-skip-permissions` inside.
"""


DEFAULT_SKILLS: dict[str, str] = {
    "send-message.md": DEFAULT_SKILL_SEND_MESSAGE,
    "upload-file.md": DEFAULT_SKILL_UPLOAD_FILE,
    "attachments.md": DEFAULT_SKILL_ATTACHMENTS,
    "permissions.md": DEFAULT_SKILL_PERMISSIONS,
}


def ensure_shared_primer(shared_dir: Path) -> None:
    """Create ``shared_dir`` and seed it with default content on
    first use. Idempotent — never overwrites existing files so
    operator edits to the primer / skills survive.
    """
    shared_dir.mkdir(parents=True, exist_ok=True)
    primer = shared_dir / "CLAUDE.md"
    if not primer.exists():
        primer.write_text(DEFAULT_SHARED_CLAUDE_MD, encoding="utf-8")
    readme = shared_dir / "README.md"
    if not readme.exists():
        readme.write_text(DEFAULT_SHARED_README, encoding="utf-8")
    skills_dir = shared_dir / "skills"
    skills_dir.mkdir(exist_ok=True)
    for name, body in DEFAULT_SKILLS.items():
        path = skills_dir / name
        if not path.exists():
            path.write_text(body, encoding="utf-8")


def sync_shared_skills(shared_dir: Path, workspace_dir: Path) -> None:
    """Mirror ``shared/skills/*.md`` into
    ``<workspace>/.claude/skills/`` so Claude Code (cli-docker,
    cli-local) auto-discovers them and the SDK adapter's project-
    scope lookup picks them up. Always overwrites so operator edits
    to the shared skills propagate on the next worker restart.
    """
    src = shared_dir / "skills"
    if not src.is_dir():
        return
    dst = workspace_dir / ".claude" / "skills"
    dst.mkdir(parents=True, exist_ok=True)
    for path in src.glob("*.md"):
        try:
            (dst / path.name).write_text(
                path.read_text(encoding="utf-8"),
                encoding="utf-8",
            )
        except OSError:
            # Non-fatal — skills are a nice-to-have, don't break the
            # worker startup if the copy fails.
            continue


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
