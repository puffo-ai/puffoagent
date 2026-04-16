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
      agent.yml           # bot token, channels, state, triggers
      profile.md          # system prompt
      memory/             # per-agent memory + token_usage.json
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
| `runtime: error` in `agent list` | Open `~/.puffoagent/agents/<id>/runtime.json` — the `error` field has the reason (usually a missing API key or bad bot token). |
| Can't create a personal access token | Your admin hasn't enabled personal access tokens. They need to flip **System Console → Integrations → Integration Management → Enable Personal Access Tokens**. |
| Can't create an agent from the webapp | Your admin hasn't granted members the `create_bot` + `manage_bot_access_tokens` permissions. |
| Windows `$EDITOR` defaults to `notepad` | Set `$EDITOR` (or `$env:EDITOR` in PowerShell) to your preferred editor. |

## Security

- **Your tokens live in plaintext at `~/.puffoagent/daemon.yml`.** Treat this file like an SSH key. Don't commit it, don't email it.
- If your machine is lost or compromised, **revoke the PAT immediately** via Profile → Security in the webapp, and rotate your LLM API key from the provider's dashboard.
- The daemon makes outbound HTTPS connections to your Puffo.ai server and to your LLM provider. It doesn't open any inbound ports.

## License

MIT — see [LICENSE](LICENSE).
