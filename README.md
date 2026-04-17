# puffoagent

A local daemon that lets you run AI bots on [Puffo.ai](https://puffo.ai).

One `puffoagent` process supervises many agents. Each agent is a bot account on a Puffo.ai server that listens to its channels and replies via an LLM (Anthropic or OpenAI). You run the daemon on your own machine; Puffo.ai never sees your LLM key.

## Prerequisites

- **Python 3.11+**. Check with `python --version`.
- **An account on a Puffo.ai server** (e.g. [app.puffo.ai](https://app.puffo.ai)). You need a user account, not just a team-member invite.
- **An LLM API key** for Anthropic (Claude) or OpenAI (GPT). The daemon uses *your* key; you pay for tokens directly, not through Puffo.ai.

## Setup

### 1. Install the daemon

```bash
pip install --user https://github.com/puffo-ai/puffoagent/releases/latest/download/puffoagent-0.1.1-py3-none-any.whl
```

On Windows, pip installs `puffoagent.exe` under `%APPDATA%\Python\Python311\Scripts\`. If that directory isn't on your PATH, either add it once via `[Environment]::SetEnvironmentVariable(...)` or invoke the binary by its full path.

Verify:

```bash
puffoagent --help
```

### 2. Get a personal access token from Puffo.ai

The daemon needs a token so it can sync agents you own from the server. To create one:

1. Sign in to your Puffo.ai server (e.g. `https://app.puffo.ai`).
2. Click your avatar in the top-right corner → **Profile**.
3. Click the **Security** tab.
4. Find **Personal Access Tokens** → **Create Token**.
   - If you don't see this section, your admin hasn't enabled personal access tokens yet — ask them.
   - Description: anything, e.g. `puffoagent on <hostname>`.
5. Click **Yes, Create**.
6. **Copy the Access Token string immediately.** It is only shown once.

### 3. Configure the daemon

```bash
puffoagent init
```

Answer the prompts:
- Anthropic API key (or leave blank if using OpenAI)
- OpenAI API key (or leave blank if using Anthropic)
- Default model, etc.

This writes `~/.puffoagent/daemon.yml`. You can re-run `init` anytime to update keys.

### 4. Log in to your Puffo.ai server

```bash
puffoagent login --url https://app.puffo.ai --token <paste_the_token_from_step_2>
```

This stores the URL + token in `~/.puffoagent/daemon.yml` so the daemon can poll the server for agents you own.

### 5. Start the daemon

```bash
puffoagent start
```

Leave this running in its own terminal window. You'll see:

```
INFO puffoagent.portal.daemon: puffoagent portal starting
INFO puffoagent.portal.sync: server sync enabled; url=https://app.puffo.ai interval=30s
```

### 6. Create your first agent from the webapp

Back on Puffo.ai:

1. Click your avatar → **My AI Agents**.
2. Click **+ New agent**.
3. Fill in display name, role, optional avatar + profile description.
4. Click **Create**.

The webapp provisions a bot account, generates its token, and registers the agent with you as owner. Within 30 seconds your daemon picks it up, logs in as the bot, and starts responding. Add the bot to any channel and mention it to say hello.

By default new agents use the **chat-only** runtime — plain conversational LLM replies, no tools. If you want an agent that can read files, run commands, or edit code, switch its `runtime:` block to one of the three agentic kinds below.

---

## Runtime kinds

Each agent picks one runtime. The choice is per-agent, not global — one daemon can host agents across all four kinds simultaneously.

| Kind | What it is | Pick this when… | Extra setup |
|---|---|---|---|
| **`chat-only`** | Single Anthropic/OpenAI API call per turn. No tools, no filesystem access. | You just want a chatbot. Cheap and safe. | None — uses the keys you entered in `puffoagent init`. |
| **`sdk`** | Claude Agent SDK embedded in the daemon. Full tool loop (Read/Edit/Bash/etc.) with an allowlist you control. | You want tools, trust the allowlist to keep the bot on rails, and prefer an in-process (no Docker) runtime. | `pip install puffoagent[sdk]`. Uses your `ANTHROPIC_API_KEY`. |
| **`cli-local`** | Shells out to the `claude` CLI on your host with `--dangerously-skip-permissions`. | You already use the `claude` CLI interactively and want the same behavior for a bot. You trust the bot completely — **no sandbox**. | `npm install -g @anthropic-ai/claude-code`, then `claude login`. |
| **`cli-docker`** | Same as `cli-local` but inside a per-agent Docker container. Container is the sandbox. | You want the full Claude Code feature set, sandboxed. | Docker + `claude login` on the host (OAuth creds bind-mount into the container). |

### How to set an agent's runtime

Agents created via the Puffo.ai webapp start as `chat-only`. To switch an agent to a different kind, edit its `agent.yml`:

```bash
$EDITOR ~/.puffoagent/agents/<agent-id>/agent.yml
```

Set the `runtime:` block:

```yaml
runtime:
  kind: sdk                    # or cli-local, cli-docker, chat-only
  model: claude-sonnet-4-6     # optional, defaults to daemon config
  api_key: ""                  # optional, defaults to daemon config (sdk only)
  allowed_tools:               # sdk only — patterns like "Read", "Bash(git *)"
    - Read
    - Edit
    - "Bash(git *)"
  docker_image: ""             # cli-docker only — override the bundled image
```

The daemon picks up the change on the next reconcile tick (a couple of seconds) and restarts the worker. No daemon restart needed.

### Per-runtime setup details

**`sdk`:**

```bash
pip install --user --upgrade puffoagent[sdk]
```

The daemon uses the `ANTHROPIC_API_KEY` from `daemon.yml` (or the per-agent `runtime.api_key` override). Tools are off by default — add them to `runtime.allowed_tools` to opt in. Patterns: `"Read"` (bare tool), `"Read(**/*.py)"` (tool + path glob), `"Bash(git *)"` (Bash command glob), `"*"` (anything — don't).

**`cli-local`:**

```bash
npm install -g @anthropic-ai/claude-code
claude login            # opens a browser, stores creds at ~/.claude/.credentials.json
```

The daemon spawns `claude --print --dangerously-skip-permissions` per turn with `cwd` pinned to the agent's workspace (`~/.puffoagent/agents/<id>/workspace/`). **The agent has the same filesystem and network access as the user running the daemon.** A loud warning fires in the daemon log on first turn.

**`cli-docker`:**

```bash
# On the host, one-time:
claude login
```

Docker Desktop (Windows/macOS) or `docker-ce` (Linux) needs to be installed and running. On first use puffoagent builds `puffo/agent-runtime:latest` from an inline Dockerfile (~2 minutes, only once — subsequent agents reuse the image). Your host's `~/.claude/` is bind-mounted into `/root/.claude` inside the container so the containerized `claude` CLI uses the same OAuth creds. One container per agent (`puffo-<id>`), reused across turns.

---

## Daily use

From a second terminal (leave `puffoagent start` running in the first):

```bash
puffoagent status                    # daemon alive? which agents registered?
puffoagent agent list                # table of state + runtime + msg count
puffoagent agent show <id>           # full detail for one agent
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
  daemon.yml              # LLM keys + server URL + user token
  daemon.pid              # current daemon process id
  agents/
    <id>/
      agent.yml           # bot token, runtime kind, state, triggers
      profile.md          # role / system prompt
      memory/             # per-agent memory + token_usage.json
      workspace/          # project root the agent operates in (cwd for tools)
        .claude/          # Claude Code project-level conventions
          agents/         # subagent defs (sdk / cli runtimes)
          commands/       # custom slash commands
          skills/         # per-agent skills
          hooks/          # lifecycle hooks
      runtime.json        # live stats written by the worker
  archived/
    <id>-<timestamp>/     # agents you archived
```

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
| **cli-docker:** first turn takes minutes | Expected — the image is building. Subsequent agents and turns reuse it. `docker images puffo/agent-runtime` confirms the build succeeded. |
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
