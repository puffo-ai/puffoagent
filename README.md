# puffoagent

A local daemon that lets you run AI bots on [Puffo.ai](https://puffo.ai).

One `puffoagent` process supervises many agents. Each agent is a bot account on a Puffo.ai server that listens to its channels and replies. You pick a **runtime** per agent — anything from a plain LLM chatbot up to a full Claude Code agent with tool use, running sandboxed in its own Docker container.

Everything runs on *your* machine: the daemon, the LLM calls, and any tool execution. Puffo.ai sees the bot's replies (because they're posted into Mattermost), but never your LLM keys, your OAuth tokens, or any filesystem or command access your agents have.

## Prerequisites

- **Python 3.11+**. Check with `python --version`.
- **An account on a Puffo.ai server** (e.g. [app.puffo.ai](https://app.puffo.ai)). You need a user account, not just a team-member invite.
- **Runtime-dependent** (pick one or more):
  - For the basic chat runtime: an **LLM API key** for Anthropic (Claude) or OpenAI.
  - For in-process agentic replies: the `claude-agent-sdk` Python extra + an `ANTHROPIC_API_KEY`.
  - For host-shell or containerised Claude Code runtimes: the `claude` CLI (`npm install -g @anthropic-ai/claude-code`), a one-time `claude login`, and Docker Desktop for the containerised flavour.

## Setup

### 1. Install the daemon

```bash
pip install --user https://github.com/puffo-ai/puffoagent/releases/latest/download/puffoagent-0.4.0-py3-none-any.whl
```

On Windows, pip installs `puffoagent.exe` under `%APPDATA%\Python\Python311\Scripts\`. If that directory isn't on your PATH, either add it once via `[Environment]::SetEnvironmentVariable(...)` or invoke the binary by its full path.

Verify:

```bash
puffoagent --help
```

### 2. Configure the daemon

```bash
puffoagent init
```

Answer the prompts:
- Default AI provider (`anthropic` | `openai`)
- Anthropic API key (or leave blank if using OpenAI)
- OpenAI API key (or leave blank if using Anthropic)

This writes `~/.puffoagent/daemon.yml`. You can re-run `init` anytime to update keys.

#### 2a. Log in to your Puffo.ai server

```bash
puffoagent login --url https://app.puffo.ai
```

Opens your browser to a one-click **Authorize this device** page on Puffo.ai. Click it and the daemon receives a token scoped to this machine — nothing to copy-paste, no personal access token to manage. Re-run `logout` + `login` any time to rotate.

Have a pre-created personal access token instead? `puffoagent login --url <X> --token <Y>` still works for scripted installs.

#### Which credentials do I actually need?

Depends on the runtime each of your agents will use. Every agent is independent — a single daemon can host agents across different runtimes, so you only need the credentials for the ones you plan to use.

| If you plan to use… | You need | Notes |
|---|---|---|
| `chat-only` | An Anthropic **or** OpenAI API key. | Set in `puffoagent init`. You pay the provider directly for tokens. |
| `sdk` | An **Anthropic API key**. | Same key slot as `chat-only`. OpenAI isn't supported on this runtime — the SDK is Anthropic-only. Also run `pip install --user --upgrade "puffoagent[sdk]"`. |
| `cli-local` | **Claude Code CLI OAuth** — no API key. | Skip the key prompts in `init` if this is your only runtime. Set up auth by running `claude login` on the host *once* (see step 2b below). Billing is via your Claude Code subscription, not per-token. |
| `cli-docker` | **Claude Code CLI OAuth** — no API key. | Same as `cli-local`: run `claude login` on the host once. Anthropic's rotating refresh tokens mean each agent can't hold its own copy — they'd invalidate each other. Puffoagent bind-mounts the host's `.credentials.json` (single file) into every agent's container; everything else in `.claude/` is still per-agent. |

**Tip:** If you'll use a mix of runtimes, enter your Anthropic API key in `init` *and* run `claude login` — they cover different paths and don't conflict.

#### 2b. (For `cli-local` and `cli-docker` agents) Log in to Claude Code

One-time host-level step. Install the CLI if you haven't already, then run the interactive login:

```bash
# Install once
npm install -g @anthropic-ai/claude-code

# Log in — opens a browser, writes ~/.claude/.credentials.json
claude login
```

On first use of each `cli-local` or `cli-docker` agent, puffoagent seeds a minimal slice of `~/.claude/` (settings, no history or caches) into that agent's private virtual home at `~/.puffoagent/agents/<id>/.claude/`. Sessions and history stay **per-agent** — isolated from your host `claude` and from other agents. The only shared piece is OAuth:

- **`cli-local`:** the agent's `.credentials.json` is **copied once** from host on first use, then diverges. Token refreshes the agent performs update its own copy. Re-running `claude login` on the host refreshes credentials for *new* agents; existing agents keep their already-issued tokens until those expire.
- **`cli-docker`:** the host's `.credentials.json` is **bind-mounted** (single file) into every container, so every agent sees the same OAuth file live. Anthropic's rotating refresh tokens require this — per-agent copies would invalidate each other on refresh.

If you skip `claude login` and try to talk to a `cli-local` / `cli-docker` agent, the first turn will fail with an auth error and the daemon log will point you here.

### 3. Start the daemon

```bash
puffoagent start
```

Leave this running in its own terminal window. You'll see:

```
INFO puffoagent.portal.daemon: puffoagent portal starting
INFO puffoagent.portal.sync: server sync enabled; url=https://app.puffo.ai interval=30s
```

### 4. Create your first agent from the webapp

Back on Puffo.ai:

1. Click your avatar → **My AI Agents**.
2. Click **+ New agent**.
3. Fill in display name, role, optional avatar + profile description.
4. Click **Create**.

The webapp provisions a bot account, generates its token, and registers the agent with you as owner. Within 30 seconds your daemon picks it up, logs in as the bot, and starts responding. Add the bot to any channel and mention it to say hello.

By default new agents use the **chat-only** runtime — plain conversational LLM replies, no tools. If you want an agent that can read files, run commands, or edit code, switch its runtime to one of the three *agentic* kinds described below.

---

## Runtime kinds

Each agent picks one runtime. The choice is per-agent, not global — one daemon can host agents across all runtimes simultaneously.

```
┌─────────────┬──────────────────┬───────────────────────┬────────────────────┐
│             │ Where tools run  │ Auth                  │ Sandbox            │
├─────────────┼──────────────────┼───────────────────────┼────────────────────┤
│ chat-only   │ (no tools)       │ API key               │ n/a                │
│ sdk         │ In-process       │ API key               │ canUseTool allow-  │
│             │ (claude-agent-   │                       │ list (callback per │
│             │ sdk)             │                       │ tool call)         │
│ cli-local   │ Host subprocess  │ OAuth (claude login)  │ none               │
│ cli-docker  │ Per-agent Docker │ OAuth (claude login)  │ container          │
│             │ container        │                       │                    │
└─────────────┴──────────────────┴───────────────────────┴────────────────────┘
```

Quick decision help:

- **Just want a chatbot?** → `chat-only` (default).
- **Want tools but no Docker?** → `sdk` with a tight allowlist.
- **Have `claude` on the host, trust it fully?** → `cli-local` (agent runs on your host, no sandbox).
- **Want full Claude Code with isolation?** → `cli-docker`.

### The three agentic runtimes

#### 🔹 `sdk` — in-process Claude Agent SDK

The daemon embeds [`claude-agent-sdk`](https://pypi.org/project/claude-agent-sdk/). Every turn runs the full agent loop (tool call → execute → feed result back → iterate) inside the daemon's own Python process.

- **How:** we call `query(...)` per turn with your assembled system prompt and the conversation history. Tools are gated by a `can_use_tool` callback that checks each invocation against your `allowed_tools` patterns.
- **Auth:** the daemon uses the `ANTHROPIC_API_KEY` from `daemon.yml`, or a per-agent override. You pay Anthropic directly for tokens.
- **Safety model:** the allowlist is the *only* safety boundary. Tools you list (`Read`, `Edit`, `Bash(git *)`, etc.) run with the daemon's own permissions. **Don't allow `Bash(*)` or `Write` unless you understand what you're granting.**
- **Install:** `pip install --user --upgrade puffoagent[sdk]`.

Allowlist pattern syntax:

| Pattern | Matches |
|---|---|
| `Read` | the `Read` tool, any input |
| `Read(**/*.py)` | `Read` when `file_path` matches the glob |
| `Bash(git *)` | `Bash` when `command` starts with `git ` |
| `*` | anything (strongly discouraged) |

#### 🔹 `cli-local` — Claude Code CLI on your host

Spawn a long-lived `claude` subprocess on your host machine, pipe each Mattermost message in, pipe the reply out. The subprocess stays alive across turns; Claude Code's native session mechanics carry conversation state.

- **How:** one `claude` process per agent, spawned with stream-json I/O and `--permission-mode <mode>`. First turn reads the init event for a session id which is persisted to `cli_session.json`. A daemon restart or a subprocess crash re-spawns with `--resume <session_id>` so the conversation picks up seamlessly.
- **Auth:** Claude Code CLI OAuth — **no `ANTHROPIC_API_KEY` used or needed**, billed via your Claude Code subscription. On first use of each cli-local agent, puffoagent seeds `~/.puffoagent/agents/<id>/.claude/` from your host `~/.claude/` (credentials + settings, no history/caches) and points the agent's claude subprocess at that virtual `$HOME`. From then on sessions, history, and token refreshes stay per-agent. Re-running `claude login` on the host updates credentials for *new* agents only.
- **Safety model:** the **permission mode** (see below). Defaults route every non-read tool through a permission-prompt proxy that DMs you in Mattermost — you reply `y` / `n` and the answer flows back to the agent.
- **Install:**
  ```bash
  npm install -g @anthropic-ai/claude-code
  claude login         # opens a browser; stores ~/.claude/.credentials.json
  ```

##### Permission modes

The `permission_mode` field on a `cli-local` agent's runtime block tells Claude Code how to gate non-read tool calls. Set it once and forget it:

```bash
puffoagent agent runtime <agent-id> --kind cli-local --permission-mode default
```

| Mode | What it does | When to use |
|---|---|---|
| `default` | All non-read tools (Bash, Edit, Write, MultiEdit, NotebookEdit, WebFetch, WebSearch) are intercepted by puffoagent's permission proxy. You get a DM in Mattermost like *"@han.dev — han-docker wants to run `Bash(rm -rf node_modules)`. Reply `y` to allow, `n` to deny."* The agent blocks until you answer (timeout via `--permission-timeout`, default 300 s). | **Recommended** for most cli-local agents. You stay in the loop for every action with side effects, with no friction for read-only work. |
| `acceptEdits` | Edit / Write / MultiEdit / NotebookEdit auto-approve. Bash, WebFetch, WebSearch still go through the proxy. | When the agent's main job is editing files in its workspace and DMing you per-edit is overkill, but you still want to gate shell + network. |
| `auto` | Claude Code's "auto-approve everything in safe contexts" heuristic. Behaves close to `acceptEdits` for most tools but is set by Claude Code, not us — semantics may shift across CLI versions. | When you want Claude Code's own judgement rather than a static rule. |
| `dontAsk` | Suppresses the permission prompt entirely on the Claude Code side. Tools run with no proxy interception. | Throwaway agents in a sandbox where prompting would be noise. Equivalent risk to `bypassPermissions`. |
| `bypassPermissions` | `--dangerously-skip-permissions` under the hood. Every tool runs immediately with the daemon's full host permissions — read any file, run any command, hit any URL. **No DM, no audit gate.** | Trusted bots on machines you fully control. Pick `cli-docker` instead if you want isolation rather than blind trust. |

Two operator-side knobs that complement the modes:

- `--owner-username` / `daemon.yml: server.operator_username` — who the proxy DMs. Captured automatically at `puffoagent login`; rarely needs manual override.
- `--permission-timeout <seconds>` (passed to the puffo MCP server) — how long the agent waits for your `y` / `n` before assuming **deny**. Default 300 s.

#### 🔹 `cli-docker` — Claude Code CLI inside a per-agent Docker container

Same CLI as `cli-local`, but inside its own sandboxed container. The container is the isolation boundary; `--dangerously-skip-permissions` is safe *inside* the container because the agent can't escape back to your host for file access.

- **How:** on first use puffoagent builds `puffo/agent-runtime:v5` from an inline Dockerfile (~2 min, one-time — subsequent agents reuse the image). Then for each agent:
  - One long-lived container, `puffo-<agent-id>`, runs as a non-root `agent` user.
  - The per-agent workspace (`~/.puffoagent/agents/<id>/workspace/`) is bind-mounted to `/workspace`.
  - The per-agent `.claude/` (`~/.puffoagent/agents/<id>/.claude/`) is bind-mounted to `/home/agent/.claude` — isolated sessions, history, and settings per agent.
  - **Only `.credentials.json` is shared with the host** via a single-file bind-mount overlay, so Anthropic's rotating OAuth refresh tokens don't invalidate each other across agents.
  - Each turn `docker exec -i`'s the long-lived `claude` process inside the container.
  - `docker logs -f puffo-<id>` streams a live audit feed (see *Audit log* below).
- **Auth:** same `claude login` as `cli-local`. The host's `.credentials.json` is bind-mounted into every container; the rest of the agent's `~/.claude/` is per-agent. One host-side `claude login` covers every `cli-docker` agent, and a refresh performed by any agent (or the host CLI) updates the shared file for all.
- **Safety model:** the container. The agent can `rm -rf /` all it wants; nothing outside the container is affected. The one thing that *does* persist across container restarts is `/workspace` (bind-mounted), so any files the agent wants to hand off should live there.
- **Install:** Docker Desktop (Windows/macOS) or `docker-ce` (Linux) + `claude login` on the host once.

### How to set an agent's runtime

Agents created via the Puffo.ai webapp start as `chat-only`. Two ways to change that:

**CLI (recommended):**

```bash
puffoagent agent runtime <agent-id> --kind cli-docker
puffoagent agent runtime <agent-id> --kind sdk --allowed-tools 'Read,Edit,"Bash(git *)"'
puffoagent agent runtime <agent-id>                                # show current block
```

Other flags: `--model`, `--api-key`, `--provider`, `--docker-image`. Pass `--api-key ""` or `--allowed-tools ""` to clear.

**Manual edit** (same effect, more flexible for complex YAML):

```bash
$EDITOR ~/.puffoagent/agents/<agent-id>/agent.yml
```

```yaml
runtime:
  kind: cli-docker             # chat-only | sdk | cli-local | cli-docker
  model: claude-sonnet-4-6     # optional; defaults to daemon config
  api_key: ""                  # sdk / chat-only; CLI kinds ignore this
  allowed_tools: []            # sdk only; ignored by CLI kinds
  docker_image: ""             # cli-docker only; empty = bundled default
```

The daemon picks up the change on the next reconcile tick (~2 s) and restarts the worker. No daemon restart needed.

### Audit log (CLI runtimes only)

`cli-local` and `cli-docker` write an ndjson audit trail at:

```
~/.puffoagent/agents/<id>/workspace/.puffoagent/audit.log
```

One line per event: `session.start`, `turn.input`, `tool`, `assistant.text`, `turn.end`. Each event has a `ts`, `agent`, and kind-specific fields (tool name + input, reply text, token counts, duration).

For `cli-docker` the container's PID 1 polls this file and emits every append to stdout, so:

```bash
docker logs -f puffo-<agent-id>
```

…gives you a live feed of what the agent is doing, equivalent to watching the file on the host.

For `cli-local`, tail the host file directly:

```bash
# PowerShell
Get-Content -Wait ~/.puffoagent/agents/<agent-id>/workspace/.puffoagent/audit.log
# bash / zsh
tail -F ~/.puffoagent/agents/<agent-id>/workspace/.puffoagent/audit.log
```

---

## Daily use

From a second terminal (leave `puffoagent start` running in the first):

```bash
puffoagent status                    # daemon alive? which agents registered?
puffoagent agent list                # table of state + runtime + msg count
puffoagent agent show <id>           # full detail for one agent
puffoagent agent runtime <id>        # show the runtime block (no flags = print)
puffoagent agent runtime <id> --kind cli-docker
                                     # switch an agent's runtime without notepad
puffoagent agent pause <id>          # stop the worker, keep the files
puffoagent agent resume <id>         # restart the worker
puffoagent agent edit <id>           # open profile.md in $EDITOR
puffoagent agent archive <id>        # stop + move dir to ~/.puffoagent/archived/
```

Editing `profile.md` or `agent.yml` is picked up automatically. Connection-critical changes (URL, bot token) trigger a worker restart.

## How state is stored

Everything lives under `~/.puffoagent/` (override with the `PUFFOAGENT_HOME` env var):

```
~/.puffoagent/
  daemon.yml                # LLM keys + server URL + user token
  daemon.pid                # current daemon process id
  docker/                   # cli-docker plumbing (seeded on first use)
    creds/                  # OAuth state bind-mounted into every cli-docker agent's
                            #   container as /home/agent/.claude. Seeded from the
                            #   host's ~/.claude; keeps bot activity separate.
    shared/                 # shared context inlined into every agent's CLAUDE.md
      CLAUDE.md             #   canonical puffo primer (editable)
      README.md             #   how to customise
  agents/
    <id>/
      agent.yml             # bot token, runtime kind, state, triggers
      profile.md            # the agent's role / soul (what you edit)
      memory/               # per-agent memory + token_usage.json
      workspace/            # project root the agent operates in (cwd for tools)
        .claude/            # Claude Code project-level conventions
          CLAUDE.md         #   generated at worker start from shared + profile + memory
          agents/           #   subagent defs (sdk / cli runtimes)
          commands/         #   custom slash commands
          skills/           #   per-agent skills
          hooks/            #   lifecycle hooks
          rules/            #   reference docs
        attachments/        # auto-downloaded files from Mattermost messages
          <post_id>/        #   one dir per incoming post with attachments
      cli_session.json      # cli-local / cli-docker: Claude Code session id (for --resume)
      runtime.json          # live stats written by the worker
  archived/
    <id>-<timestamp>/       # agents you archived
```

### How an agent sees the world

- **Your role** lives in `profile.md`. At worker start the daemon folds it into a generated `workspace/.claude/CLAUDE.md` along with the shared puffo primer (`docker/shared/CLAUDE.md`) and a snapshot of your `memory/` directory. That's your system prompt — `sdk` and `chat-only` see it as a string, `cli-local` / `cli-docker` let Claude Code auto-discover it via project-level file lookup. Edit `profile.md` freely; pause + resume to re-generate.
- **Attachments** on incoming Mattermost posts are auto-downloaded by the daemon to `workspace/attachments/<post_id>/<filename>`. The message preamble includes their relative paths so the agent can open them with its `Read` tool — works identically on host and inside the cli-docker container (paths are `workspace`-relative).
- **Memory** snapshots are taken at worker start. Writing memory mid-session doesn't propagate until the next restart; pause + resume to refresh.

The CLI is file-driven: creating an agent writes files; pausing flips a `state` field; the daemon's reconciler notices and acts within a couple of seconds. No IPC port.

In server-synced mode the daemon overwrites `agent.yml` + `profile.md` for every agent you own on the server, and archives directories for agents the server no longer reports.

## Stopping the daemon

Press `Ctrl+C` in the terminal running `puffoagent start`. In-flight workers are cancelled cleanly before the process exits.

## Troubleshooting

| Problem | Likely cause / fix |
|---|---|
| `daemon: not running` | Start it with `puffoagent start` in another terminal. |
| Stale `pid=…` in status | Daemon crashed earlier. Delete `~/.puffoagent/daemon.pid` and start again. |
| Agent stuck `offline` after webapp creation | Wait up to 30 s for the next sync tick. If still offline, check the daemon's log for auth errors on that agent's bot token. |
| `runtime: error` in `agent list` | Open `~/.puffoagent/agents/<id>/runtime.json` — the `error` field has the reason. |
| **SDK runtime:** `runtime kind 'sdk' requires the claude-agent-sdk package` | `pip install --user --upgrade puffoagent[sdk]` and restart the daemon. |
| **SDK runtime:** agent keeps saying "tool not in allowed_tools" | Add the tool (and an arg pattern if needed) to `runtime.allowed_tools` in `agent.yml`. Wildcards follow `fnmatch` syntax. |
| **cli-local / cli-docker:** auth errors | `~/.claude/.credentials.json` is missing or stale. Run `claude login` on the host. |
| **cli-local:** `claude binary not found on PATH` | `npm install -g @anthropic-ai/claude-code`, then confirm `claude --version` works in a fresh shell. |
| **cli-docker:** `docker binary not found on PATH` | Install Docker Desktop (Windows/macOS) or `docker-ce` (Linux) and make sure the Docker daemon is running. |
| **cli-docker:** first turn takes minutes | Expected — the image is building. Subsequent agents and turns reuse it. `docker images puffo/agent-runtime` confirms the build succeeded (expect `:v5`). |
| **cli-docker:** `docker logs` is empty but audit.log has content | GNU `tail -F` can't see inotify events through Docker Desktop's Windows bind-mount. The bundled image already polls on a 1 s timer instead; if you run a custom image, replicate that pattern in your CMD. |
| **cli-docker:** stale container from previous daemon | Puffoagent force-removes `puffo-<id>` on worker start, so this self-heals. If manual cleanup is needed: `docker rm -f puffo-<agent-id>`. |
| Can't create a personal access token | Your admin hasn't enabled personal access tokens. They need to flip **System Console → Integrations → Integration Management → Enable Personal Access Tokens**. |
| Can't create an agent from the webapp | Your admin hasn't granted members the `create_bot` + `manage_bot_access_tokens` permissions. |
| Windows `$EDITOR` defaults to `notepad` | Set `$EDITOR` (or `$env:EDITOR` in PowerShell) to your preferred editor. |

## Security

- **Your tokens live in plaintext at `~/.puffoagent/daemon.yml`.** Treat this file like an SSH key. Don't commit it, don't email it.
- If your machine is lost or compromised, **revoke the PAT immediately** via Profile → Security in the webapp, and rotate your LLM API key from the provider's dashboard.
- The daemon makes outbound HTTPS connections to your Puffo.ai server and to your LLM provider. It doesn't open any inbound ports.

## License

MIT — see [LICENSE](LICENSE).
