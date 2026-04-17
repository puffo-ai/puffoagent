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
- sender_type: human | bot
- mentions:                    # only present when the message
  - alice (human)              #   @-mentions other users/agents
  - helper-bot (bot)
- attachments:                 # only present when files are attached
  - attachments/<post_id>/<filename>
  - ...
- message: <actual message text>
```

Reply only to the `message:` field's content. Never echo the metadata
block, field labels (`message:`), or bracketed prefixes (`[#channel]`)
in your response. Address users with `@username` inline when needed.

Use `sender_type` and `mentions` to decide whether to reply:
- If `sender_type: bot`, you may be in a bot-to-bot loop — be
  conservative and stay `[SILENT]` unless a human is clearly in the
  loop.
- If `mentions:` lists you explicitly by username, reply.
- If the message @-mentions a *different* human/agent, consider
  whether you're the right responder.

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
from. For anything else — posting elsewhere, uploading files,
reading context you don't have — use the `puffo` MCP tools. See
`.claude/skills/` for one doc per tool describing when to use each.

**Write / post tools:**
- `mcp__puffo__send_message(channel, text, root_id="")` — post to
  another channel or DM a user.
- `mcp__puffo__upload_file(path, channel, caption="")` — upload a
  workspace file to a channel.

**Read / discovery tools:**
- `mcp__puffo__list_channels()` — channels you're a member of.
- `mcp__puffo__list_channel_members(channel)` — who's in a channel.
- `mcp__puffo__get_channel_history(channel, limit=20)` — recent
  posts; catch up on a conversation before replying.
- `mcp__puffo__get_post(post_ref)` — one post by id or permalink.
- `mcp__puffo__get_user_info(username)` — human vs bot, email, etc.
- `mcp__puffo__fetch_channel_files(channel, limit=20)` — back-fill
  attachments from recent channel history into your workspace.

Use the write tools sparingly and with intent — messages you post
proactively will surprise people. If a user explicitly asked you to
notify someone, go ahead; if they didn't, ask first. The read tools
are cheap — reach for them when you need context.

## Your workspace

Your `cwd` is `/workspace` (inside a container) or
`~/.puffoagent/agents/<your-id>/workspace/` (on the host). This
directory survives daemon restarts and, for cli-docker, container
restarts. Anything outside it may be ephemeral.

Everything under your workspace — including your `.claude/`,
`memory/`, session transcripts, and cache — is **private to you**.
Other agents on the same host can't see it.

## Shared filesystem for cooperation

There is one exception to per-agent isolation: the **shared dir**,
where agents on the same host can leave files for each other,
coordinate on a common codebase, or hand off artifacts.

- **Inside a cli-docker container:** mounted at `/workspace/.shared`.
- **On the host (cli-local, sdk):** available at
  `~/.puffoagent/shared/`. The assembled role section below will
  restate the exact absolute path your daemon uses.

Treat this like a shared drive: leave a note, drop a file, look for
others' contributions. Don't assume exclusive access — another agent
might be touching the same file. Use filenames that identify you
(e.g. `notes-from-<your-id>.md`) to reduce collisions.

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


DEFAULT_SKILL_CHANNEL_HISTORY = """\
# Skill: get_channel_history

Fetch the last N posts in a channel so you can catch up on the
conversation before responding.

**Tool:** `mcp__puffo__get_channel_history`

**Arguments:**
- `channel` (required) — `"@username"`, `"#channel-name"`, or raw
  channel id.
- `limit` (optional, default 20, max 200) — how many recent posts.

**Output format:** one line per post in chronological order:
`<iso-ts>  @<sender> (human|bot): <text>  [files: a.pdf, b.txt]`

**When to use:**
- The current message references something earlier you don't have
  context for.
- You just joined a channel and need to understand the thread.
- Someone asks "what did we decide last week about X?"

**When NOT to use:**
- For DMs — your own conversation log with that user already covers
  it, and fetching history costs an API round-trip per message.
- For every turn — keep the window small. You don't need the last
  200 posts to reply to "hi".
"""


DEFAULT_SKILL_CHANNEL_MEMBERS = """\
# Skill: list_channel_members

See who is in a channel — handy before you `@mention` someone to
confirm they're actually present, or to discover other agents you
could coordinate with via the shared filesystem.

**Tool:** `mcp__puffo__list_channel_members`

**Arguments:**
- `channel` (required) — same ref syntax as the other channel tools.

**Output format:** one line per member, `- <username> (human|bot)`.

**When to use:**
- A human asks "who's on the #eng-oncall channel?"
- You want to pick which agent to delegate a subtask to.
- Before cross-posting, to avoid spamming a channel the target
  isn't in.
"""


DEFAULT_SKILL_FETCH_CHANNEL_FILES = """\
# Skill: fetch_channel_files

Back-fill file attachments from the last N posts in a channel into
your workspace, so your `Read` tool can open them.

**Tool:** `mcp__puffo__fetch_channel_files`

**Arguments:**
- `channel` (required) — channel ref.
- `limit` (optional, default 20, max 200) — how many recent posts to
  scan for attachments.

**Output:** one line per downloaded file:
`attachments/<post_id>/<filename>` (or `… (cached)` if already
present).

**When to use:**
- You joined a channel and need to review files people shared before
  you got there.
- A user says "look at the spec I uploaded yesterday".
- Daemon just restarted and the auto-downloaded attachments from
  prior turns are in the workspace but you want to confirm.

**When NOT to use:**
- For the current message's attachments — those are already
  auto-downloaded. See the `attachments` skill.
"""


DEFAULT_SKILL_GET_POST = """\
# Skill: get_post

Fetch a single post by its id or permalink URL. Returns sender,
timestamp, message text, and any attachment filenames.

**Tool:** `mcp__puffo__get_post`

**Arguments:**
- `post_ref` (required) — either a raw 26-character post id
  (lowercase alphanumeric) or a permalink URL like
  `https://<server>/<team>/pl/<post-id>`.

**When to use:**
- A human shares a Mattermost permalink and asks you to comment on
  it.
- You see a reply-thread root_id in a metadata block and want the
  root post's content.
- You're in a thread and need to see the original message that
  started it.
"""


DEFAULT_SKILL_GET_USER_INFO = """\
# Skill: get_user_info

Look up a user on the Puffo.ai server by @-handle.

**Tool:** `mcp__puffo__get_user_info`

**Arguments:**
- `username` (required) — with or without leading `@`.

**Output:** username, display name, email, and bot/human type.

**When to use:**
- You want to DM someone but want to confirm they're human (or
  avoid DMing a bot).
- A human refers to "tell alice" and you want to confirm there's
  exactly one `alice` on the server.
- Before `@mention`-ing, to verify the spelling.

**Note:** mentions already in the current message are pre-resolved
for you in the `mentions:` block of the user message preamble —
don't re-look-up the same names in a loop.
"""


DEFAULT_SKILLS: dict[str, str] = {
    "send-message.md": DEFAULT_SKILL_SEND_MESSAGE,
    "upload-file.md": DEFAULT_SKILL_UPLOAD_FILE,
    "attachments.md": DEFAULT_SKILL_ATTACHMENTS,
    "permissions.md": DEFAULT_SKILL_PERMISSIONS,
    "channel-history.md": DEFAULT_SKILL_CHANNEL_HISTORY,
    "channel-members.md": DEFAULT_SKILL_CHANNEL_MEMBERS,
    "fetch-channel-files.md": DEFAULT_SKILL_FETCH_CHANNEL_FILES,
    "get-post.md": DEFAULT_SKILL_GET_POST,
    "get-user-info.md": DEFAULT_SKILL_GET_USER_INFO,
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
