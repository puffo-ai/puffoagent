# puffoagent runtime adapters — design

This is the authoritative design note for the adapter rework. When the code and this doc disagree, the code wins — update this to match.

## Goal

Let each agent choose its runtime independently. Same Mattermost-facing shell, a three-dimensional type system per agent:

1. **Runtime** (`kind`) — WHERE the agent executes. `chat-local` / `sdk-local` / `cli-local` / `cli-docker`; `cli-sandbox` is reserved for a future release and rejected at load.
2. **Provider** — WHO serves the model. `anthropic` / `openai` / `google`.
3. **Harness** — WHAT agent engine runs inside the runtime. Only meaningful for CLI runtimes; `chat-local` / `sdk-local` ignore the field. `claude-code` / `hermes` / `gemini-cli` (the last is reserved — not yet implemented).

The full compatibility matrix (see `portal/runtime_matrix.py` for the single source of truth):

| Runtime | Where tools run | Providers | Harness |
|---|---|---|---|
| `chat-local` | LLM API call only, no tools | `anthropic` / `openai` / `google` | N/A |
| `sdk-local` | In-process (`claude-agent-sdk`; openai/google SDKs reserved) | `anthropic` *(shipped)* — `openai` / `google` reserved | N/A — the SDK IS the harness |
| `cli-local` | Host subprocess (non-root, stream-json) | matches harness | `claude-code` (anthropic) · `hermes` (anthropic, openai) — `gemini-cli` rejected here, use cli-docker |
| `cli-docker` | Per-agent Docker container (non-root `agent` user, UID 2000) | matches harness | `claude-code` (anthropic) · `hermes` (anthropic, openai) · `gemini-cli` (google) |
| `cli-sandbox` | *reserved for a future release* | — | — |

**Validation.** The (runtime, provider, harness) triple is validated at `AgentConfig.load()` and at `puffoagent agent runtime` setter time. Mismatches like `harness=claude-code` + `provider=google` get rejected with a clear error naming the conflicting fields. `cli-sandbox` is the one remaining reserved slot and returns a distinct "reserved — not yet implemented" message so operators know it's a roadmap item rather than a typo.

**Legacy kind names** (`chat-only` → `chat-local`, `sdk` → `sdk-local`) are auto-migrated on load with a one-time WARNING. The shim stays in the 0.7.x line and is removed in 0.8.0.

**Auth per harness:**
- `claude-code` — OAuth tokens written by `claude login` on the host; shared across every agent via a bind-mounted / symlinked `.credentials.json`. No API key is injected by the adapter.
- `hermes` — reads the Claude Code OAuth token off the same shared credentials file and passes it to hermes as `ANTHROPIC_API_KEY` on `docker exec`. No separate hermes login.
- `gemini-cli` — static `GEMINI_API_KEY` from the daemon's `google.api_key` (set via `puffoagent init` or by editing `daemon.yml`). Passed to the container per turn as `docker exec -e GEMINI_API_KEY=...`. Google API keys don't rotate, so no refresh loop; if the key changes the operator edits `daemon.yml` and the next turn picks up the new value.

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
│ Adapter (agent/adapters/*)                                   │
│   • thin translation layer to the underlying runtime         │
│   • four implementations: ChatOnly, SDK, LocalCLI, DockerCLI │
│   • CLI adapters wrap a pluggable Harness (claude-code |     │
│     hermes) so the same adapter handles multiple engines     │
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
  kind: chat-local | sdk-local | cli-local | cli-docker

  # which model provider to use (validated against harness for CLI kinds)
  provider: anthropic          # anthropic | openai | google

  # which agent engine runs inside the runtime (cli-local / cli-docker only)
  harness: claude-code         # or: hermes | gemini-cli (reserved)

  # shared — applicable to every kind
  model: claude-sonnet-4-6
  api_key: ""                  # empty → inherit from daemon.yml

  # sdk / cli-local / cli-docker
  allowed_tools: [Read, Edit, "Bash(git *)"]

  # sdk only — cap on agentic-loop iterations within one turn
  max_turns: 10

  # cli-local only — Claude Code permission mode. `default` routes
  # non-read tool calls through the Mattermost-owner proxy via a
  # PreToolUse hook; `acceptEdits` auto-approves Edit/Write; the
  # three skip-prompt modes (auto / dontAsk / bypassPermissions)
  # leave the agent un-sandboxed on the host.
  permission_mode: default

  # cli-docker only — override image tag; empty uses the bundled Dockerfile
  docker_image: ""
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

Skills have an extra twist: the daemon also has a *shared* `skills_dir` in `daemon.yml` that applies to every agent. For the chat-local adapter the shell's `SkillsLoader` merges both dirs (daemon-wide first, per-agent second, per-agent wins on filename collision). The three tool-running adapters pick up only the per-agent `.claude/skills/` through native discovery; users who want agent-independent skills should put them there or symlink.

## Adapter details

### Chat-local adapter (`chat-local`)

- No tool surface — plain chat completions against Anthropic / OpenAI / Google.
- Default kind for brand-new agents created via the webapp; operator flips it to `sdk-local`/`cli-local`/`cli-docker` via `puffoagent agent runtime --kind ...` when ready.
- Used for low-ceremony bots that just need to reply in channels without filesystem/network access.

### SDK-local adapter (`sdk-local`)

- Depends on `claude-agent-sdk>=0.1.61` (declared in the `sdk` optional-extra of `pyproject.toml`).
- Calls `query(prompt=..., options=ClaudeAgentOptions(system_prompt=ctx.system_prompt, cwd=workspace_dir, setting_sources=["project"], permission_mode=..., can_use_tool=self._gate, allowed_tools=[], max_turns=..., mcp_servers=...))`.
- `_gate` implements the `canUseTool` callback: matches `runtime.allowed_tools` patterns against the full tool *input* (which is why `allowed_tools=[]` is passed — populating it would auto-approve by bare name before the gate ran).
- Progress events: for each `ToolUseBlock` in the stream, call `ctx.on_progress(f"🔨 {tool_name}")`.

### Local CLI adapter (`cli-local`)

- Verifies the `claude` binary is on PATH on first turn and sanitises `permission_mode` (unknown values fall back to `default` with a WARNING).
- Links or copies `~/.claude/.credentials.json` into the per-agent `.claude/` dir so rotating OAuth refresh tokens stay consistent across the operator and every agent.
- Each turn: long-lived `claude --permission-mode <mode> --model <...> --input-format stream-json --output-format stream-json --verbose --session-id <...> --mcp-config <per-agent>` subprocess, kept alive across turns and resumed on daemon restart via the persisted `cli_session.json`. See `cli_session.py`.
- Permission proxying: a PreToolUse hook script DMs the Mattermost *operator* (the user who ran `puffoagent login`) before every matched tool call and blocks the call until the operator replies `yes`/`no` in-thread. The hook matcher is tailored to the agent's `permission_mode` (see `_hook_matcher_for_mode` in `adapters/local_cli.py`).
- Startup log level reflects the mode: INFO under `default`/`acceptEdits` (tool calls are gated), WARNING under `auto`/`dontAsk`/`bypassPermissions` (agent has full host access).

### Docker CLI adapter (`cli-docker`)

- On first use, if the image tag is absent locally, builds from the inline Dockerfile string in `adapters/docker_cli.py`. Concurrent builds across workers are serialised via a process-wide asyncio lock.
- Container runs as a non-root `agent` user (UID 2000) inside the container; the container itself is the sandbox.
- Per-agent, six bind-mounts: `workspace → /workspace`, `.claude dir → /home/agent/.claude`, host's `~/.claude/.credentials.json → /home/agent/.claude/.credentials.json` (single-file overlay — the rotating-refresh-token lives in one canonical place), `.claude.json → /home/agent/.claude.json`, shared-fs → `/workspace/.shared`, MCP script dir → read-only. No `ANTHROPIC_API_KEY` is injected for the `claude-code` harness; the `hermes` harness reads the access token out of the bind-mounted credentials file and passes it via `docker exec -e ANTHROPIC_API_KEY=...` because hermes' auto-discovery is unreliable on fresh containers.
- Each turn (claude-code harness): `docker exec -i puffo-<id> claude --input-format stream-json --output-format stream-json ...` sharing the same long-lived stream-json session as `cli-local`.
- Each turn (hermes harness): fresh `docker exec puffo-<id> hermes chat --provider anthropic --quiet [--continue] -q <prompt>` — hermes interactive mode needs a TTY which doesn't survive piped stdin, so we rely on hermes' on-disk session store + `--continue` for multi-turn continuity.
- Container lifecycle: one per agent, reused across turns. PID 1 polls the audit log and streams appends to `docker logs` for live visibility.
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

## Historical rollout order

All four adapters + harness abstraction have shipped. Kept here for context on how the design landed:

1. Shell refactor + `chat-only` adapter. (Promoted from migration seam to first-class — it remains the default kind for new agents, renamed to `chat-local` in 0.7.0.)
2. SDK adapter (`sdk`, renamed to `sdk-local` in 0.7.0) with `canUseTool` gate + pattern-glob allowlist.
3. Docker CLI adapter.
4. Local CLI adapter with PreToolUse hook permission proxy.
5. Unified skills/memory translation across adapters.
6. `Harness` abstraction — decouples the agent engine (claude-code / hermes) from the runtime kind.
7. **v0.7.0**: provider promoted to first-class, runtime matrix + validator, `cli-sandbox` / `gemini-cli` reserved, kind names unified.

## Resolved questions

- **SDK version pin:** `claude-agent-sdk>=0.1.61`, declared in the `[project.optional-dependencies]` `sdk` extra in `pyproject.toml`.
- **MCP server agent correlation:** each worker spawns its own MCP stdio subprocess with a `PUFFO_AGENT_ID` env var; tools read the env for per-agent state. No cross-agent authentication needed.
- **Permission-proxy delivery:** DMs in Mattermost to the *operator* (the user who ran `puffoagent login`), threaded by turn, reply-gated via a PreToolUse hook. Interactive message buttons were evaluated but rejected — plain DMs are simpler and don't depend on a specific Mattermost version.
- **Docker on Windows:** Docker Desktop + WSL2 backend required; documented in the README prerequisites.
