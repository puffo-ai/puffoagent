# puffoagent runtime adapters — design

This is the authoritative design note for the adapter rework. When the code and this doc disagree, the code wins — update this to match.

## Goal

Let each agent choose its runtime independently. Same Mattermost-facing shell, three pluggable backends:

| Kind | Where tools run | Auth | Permission model | Extra deps |
|---|---|---|---|---|
| `sdk` | In-process (`claude-agent-sdk`) | `ANTHROPIC_API_KEY` (from `daemon.yml` or `agent.yml`) | `canUseTool` allowlist from `agent.yml` | Python `claude-agent-sdk` package |
| `cli-local` | Host machine (`claude --print`) | OAuth — user runs `claude login` once on the host; creds live at `~/.claude/.credentials.json` | `--dangerously-skip-permissions` (no sandbox). MCP permission bridge deferred — see follow-up task #38. | `claude` binary on PATH |
| `cli-docker` | Inside a per-agent Docker container | OAuth — host's `~/.claude` is bind-mounted to `/root/.claude` so the container's claude uses the host user's OAuth creds | `--dangerously-skip-permissions` (container is the sandbox) | Docker on host |

## Non-goals

- No backward compatibility with the pre-adapter `ai:` block. Users re-run `puffoagent init` and re-create agents.
- No "auto-pick the runtime for you" heuristic. Users declare the kind in `agent.yml`.
- No hybrid modes (e.g. "SDK with fallback to CLI"). One kind per agent.

## Responsibility split

```
┌──────────────────────────────────────────────────────────────┐
│ Worker (portal/worker.py) — unchanged                        │
│   • Mattermost WebSocket listen loop                         │
│   • typing indicators, reconnect backoff                     │
│   • runtime.json heartbeat                                   │
└───────────────────────┬──────────────────────────────────────┘
                        │ on_message(...)
┌───────────────────────▼──────────────────────────────────────┐
│ PuffoAgent (agent/core.py) — slimmed                         │
│   • conversation log (per-agent, all channels)               │
│   • memory manager                                           │
│   • usage tracker                                            │
│   • system-prompt assembly                                   │
│   • delegates the turn to an Adapter                         │
└───────────────────────┬──────────────────────────────────────┘
                        │ adapter.run_turn(ctx)
┌───────────────────────▼──────────────────────────────────────┐
│ Adapter (agent/adapters/*) — this PR                         │
│   • thin translation layer to the underlying runtime         │
│   • three implementations: SDK, LocalCLI, DockerCLI          │
└───────────────────────┬──────────────────────────────────────┘
                        │ invokes
┌───────────────────────▼──────────────────────────────────────┐
│ Runtime (external, NOT ours)                                 │
│   • owns the agentic loop (tool pick → exec → iterate)       │
│   • owns the tool catalog (Read/Edit/Bash/Glob/Grep/...)     │
│   • owns LLM streaming and context window management         │
│   • = claude-agent-sdk package, or the `claude` binary       │
└──────────────────────────────────────────────────────────────┘
```

**Adapter responsibilities** (what we write):
1. Translate `TurnContext` → runtime's native invocation (SDK `QueryOptions`, or CLI argv + env).
2. Translate runtime output stream → `TurnResult` + `on_progress` callbacks.
3. Manage the runtime instance's lifecycle (spawn/reuse/teardown subprocess, container, or SDK session).
4. Wire in auxiliary services the runtime needs (MCP permission bridge for `cli-local`, volume mounts for `cli-docker`).
5. Inject policy where the runtime lets us — e.g. SDK's `canUseTool` callback for the allowlist. This is the only seam where adapter code participates *in* the loop.

Adapters do NOT: run the loop themselves, implement tools, manage memory/usage/logs, or touch Mattermost. Those either live in the shell above or in the runtime below.

## Adapter interface

```python
# agent/adapters/base.py
from dataclasses import dataclass
from typing import AsyncIterator, Callable

@dataclass
class TurnContext:
    system_prompt: str                 # profile + memory + skills, rendered
    messages: list[dict]               # {"role": "user"|"assistant", "content": str}
    workspace_dir: str                 # ~/.puffoagent/agents/<id>/workspace
    claude_dir: str                    # ~/.puffoagent/agents/<id>/.claude
    memory_dir: str                    # ~/.puffoagent/agents/<id>/memory
    on_progress: Callable[[str], None] # optional callback for "🔨 running X" updates

@dataclass
class TurnResult:
    reply: str                         # "" means the adapter chose to stay silent
    input_tokens: int
    output_tokens: int
    tool_calls: int

class Adapter:
    async def run_turn(self, ctx: TurnContext) -> TurnResult: ...
    async def aclose(self) -> None: ...   # tear down docker / mcp servers
```

`run_turn` is `async` (unlike today's blocking `provider.complete`). The worker awaits it directly; no more `asyncio.to_thread`.

## `agent.yml` schema

```yaml
id: hermes
state: running
display_name: Hermes
created_at: 1234567890

mattermost:
  url: https://app.puffo.ai
  bot_token: ...
  team_name: ...

profile: profile.md
memory_dir: memory
claude_dir: .claude           # new — skills/commands/hooks for this agent
workspace_dir: workspace      # new — files the agent reads/writes

triggers:
  on_mention: true
  on_dm: true

runtime:
  kind: sdk | cli-local | cli-docker

  # shared — applicable to every kind
  model: claude-sonnet-4-6
  api_key: ""                 # empty → inherit from daemon.yml

  # sdk-only
  allowed_tools: [Read, Edit, "Bash(git *)"]

  # cli-local-only — DEFERRED to task #38 (permission proxy):
  #   permission_timeout_seconds, permission_default, auto_approve
  # Shipped behaviour today: dangerous mode, no sandbox. A loud
  # logger.warning fires on first turn.

  # cli-docker-only
  docker:
    image: puffo/agent-runtime:latest   # set to "" to use bundled Dockerfile
    memory_mb: 2048
    cpu: 1.0
```

## On-disk layout per agent

```
~/.puffoagent/agents/<id>/
  agent.yml
  profile.md            # role definition (system prompt)
  memory/               # managed by MemoryManager
    token_usage.json
  workspace/            # project root the agent operates in (cwd for all adapters)
    .claude/            # Claude Code project-level conventions (seeded by worker on startup)
      agents/           # subagent definitions
      commands/         # custom slash commands
      skills/           # per-agent skills
      hooks/            # lifecycle hooks
  runtime.json          # written by worker
```

`.claude/` lives *inside* `workspace/` by design — that's where Claude Code's project-level discovery looks (`<cwd>/.claude/...`). Keeping it there means every adapter finds it via the same convention and we don't have to plumb a second path through. Not user-configurable.

Each adapter maps these paths to its runtime's native conventions:

- **SDK**: `cwd=workspace_dir`, `setting_sources=["project"]` → SDK discovers `.claude/CLAUDE.md`, skills, and settings natively.
- **LocalCLI**: subprocess spawned with `cwd=workspace_dir` → `claude` CLI discovers `.claude/` the same way.
- **DockerCLI**: single bind-mount `workspace_dir` → `/workspace` in the container (with `/workspace` as WORKDIR). `.claude/` rides along because it's nested inside the mounted dir.

Skills have an extra twist: the daemon also has a *shared* `skills_dir` in `daemon.yml` that applies to every agent. For the chat-only adapter the shell's `SkillsLoader` merges both dirs (daemon-wide first, per-agent second, per-agent wins on filename collision). The three tool-running adapters pick up only the per-agent `.claude/skills/` through native discovery; users who want agent-independent skills should put them there or symlink.

## Adapter details

### SDK adapter (`sdk`)

- Depends on `claude-agent-sdk>=0.0.5` (pin TBD at implementation time).
- Calls `query(prompt=..., options=QueryOptions(system_prompt=SystemPromptPreset(type="preset", preset="claude_code"), cwd=workspace_dir, setting_sources=["project"], permission_mode="default", can_use_tool=self._gate))`.
- `_gate` implements the `canUseTool` callback: checks `runtime.allowed_tools` patterns against the requested tool, returns `{"behavior": "allow"}` or `{"behavior": "deny", "message": ...}`.
- Progress events: for each `ToolUseBlock` in the stream, call `ctx.on_progress(f"🔨 {tool_name}(...)")`.

### Local CLI adapter (`cli-local`) — shipped v0: dangerous mode

- Verifies the `claude` binary is on PATH on first turn; emits a one-time `logger.warning` naming the agent and reminding the operator that permissions are bypassed.
- Also warns if `~/.claude/.credentials.json` is missing — the claude CLI reads OAuth creds from there; we do not inject `ANTHROPIC_API_KEY`.
- Each turn: `claude --print --dangerously-skip-permissions --append-system-prompt <profile> <history+message>` with `cwd=workspace_dir`. No env tampering.
- **No sandbox.** The agent has whatever filesystem + network access the daemon process has.

**Follow-up (task #38):** MCP-backed permission proxy. Introduce a local `puffo_permission_mcp` server shared across `cli-local` agents; pass `--permission-prompt-tool` to the CLI; route tool approvals to the Mattermost owner as interactive messages with timeout + `auto_approve` patterns. Add `permission_timeout_seconds`, `permission_default`, `auto_approve` fields to `RuntimeConfig` when that lands.

### Docker CLI adapter (`cli-docker`)

- On first use, if image is empty or tagged `puffo/agent-runtime:latest` and not present locally: build from the inline Dockerfile string in `adapters/docker_cli.py`. Surface build logs to the user.
- Container runs as root — the container itself is the sandbox, and root inside a container sidesteps UID-mismatch issues on the OAuth bind-mount.
- Per-agent: `docker run -d --name puffo-<id> -v <workspace>:/workspace -v <claude_dir>:/workspace/.claude -v ~/.claude:/root/.claude <image> sleep infinity`. No `ANTHROPIC_API_KEY` — OAuth creds flow through the third mount.
- Each turn: `docker exec puffo-<id> claude --print --dangerously-skip-permissions --append-system-prompt <profile> <message>`.
- Container lifecycle: one per agent, reused across turns. Torn down on agent stop/pause/archive.
- Pre-flight: on first `_ensure_started`, warn if `~/.claude/.credentials.json` doesn't exist on the host — the user has to `claude login` once before any cli-docker agent can authenticate.

## Worker changes

Replace `build_provider(...)` in `portal/worker.py` with `build_adapter(...)` that switches on `agent_cfg.runtime.kind`. Everything else in the worker stays as-is.

`on_message` becomes:

```python
async def on_message(channel_id, ...):
    typing_task = asyncio.ensure_future(_keep_typing(...))
    try:
        result = await puffo.handle_message(channel_id, ..., text, direct)
    finally:
        typing_task.cancel()
    if result.reply:
        await client.post_message(channel_id, result.reply, root_id=root_id)
```

`PuffoAgent.handle_message` becomes `async`, awaits `adapter.run_turn(ctx)`, then updates memory/usage/log from the result.

## What ships in which adapter order

1. Shell refactor + `chat-only` adapter (wraps today's `AnthropicProvider` so nothing regresses). (#32)
2. SDK adapter. (#33)
3. Docker CLI adapter. (#34)
4. Local CLI adapter with MCP permission bridge. (#35)
5. Unified skills/memory translation. (#36)
6. CLI UX + docs. (#37)

`chat-only` is not advertised in docs — it exists only as a migration seam. Drop it once the SDK adapter is stable.

## Open questions (resolve during implementation)

- SDK version pin — what's stable on PyPI?
- How does the MCP permission server authenticate which agent is calling? Env var correlation ID seems fine but needs a test.
- Mattermost interactive message buttons — confirm the wire format for our server version.
- Docker build on Windows: Docker Desktop required, WSL2 backend. Document as prereq.
