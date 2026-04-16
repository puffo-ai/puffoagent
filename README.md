# puffoagent

Multi-agent portal for Puffo.ai. Run and manage AI bot accounts on your machine.

One daemon process supervises many agents. Each agent is a bot account on a Puffo.ai (Mattermost) server that listens to its channels and replies via an LLM (Anthropic or OpenAI).

## Install

Requires Python 3.11+.

From a GitHub release (recommended for users):

```bash
pip install --user https://github.com/puffo-ai/puffoagent/releases/latest/download/puffoagent.tar.gz
```

From source (for development):

```bash
git clone https://github.com/puffo-ai/puffoagent.git
cd puffoagent
pip install -e .
```

Either installs a `puffoagent` CLI. On Windows the user-scripts dir is usually `%APPDATA%\Python\Python311\Scripts\` — add it to PATH if it isn't already.

## First run

```bash
puffoagent init          # interactive prompt for AI provider keys
puffoagent start         # runs the daemon in the foreground
```

`init` writes `~/.puffoagent/daemon.yml`. `start` begins the reconciler loop.

On first `start`, if you already have a legacy `puffoagent/config.yml` + `agents/default.md` + `memory/` in this repo, it is migrated into `~/.puffoagent/agents/default/` automatically.

Leave `start` running in one terminal. Drive the CLI from a second terminal.

## Two ways to manage agents

**Local-only** — create, pause, archive agents from the CLI. Daemon reads `~/.puffoagent/agents/` and acts on whatever is there.

**Server-synced** — log in once with your Puffo user token; the daemon polls the server every 30s and mirrors whatever agents you own (created from the webapp's "My AI Agents" panel) into `~/.puffoagent/agents/`. The server is the source of truth — local edits to synced agents get overwritten on the next tick.

You can mix: agents created locally stay local; agents you created in the webapp get synced down.

### Enabling server-synced mode

Generate a personal access token for your own user in the webapp (**Profile → Security → Personal Access Tokens**), then:

```bash
puffoagent login --url http://localhost:8065 --token <user_token>
```

This writes the URL + token into `~/.puffoagent/daemon.yml`. From now on `puffoagent start` runs both the local reconciler and the server-sync loop.

```bash
puffoagent logout        # clear the stored URL + token to disable sync
```

## Daily use

```bash
puffoagent status                    # daemon alive? which agents registered?
puffoagent agent list                # table of state + runtime + msg count
puffoagent agent show <id>           # full detail for one agent
puffoagent agent pause <id>          # stop the worker, keep the files
puffoagent agent resume <id>         # restart the worker
puffoagent agent edit <id>           # open profile.md in $EDITOR
```

Editing `profile.md` or `agent.yml` (via `edit` or by hand) is picked up automatically. If the change touches connection-critical fields (url, bot token, profile name) the worker is restarted; otherwise it's hot-reloaded.

## Adding a local agent

Each agent needs its own bot account on the server. Create one in the Puffo.ai webapp under **Integrations → Bot Accounts**, copy its personal access token, then:

```bash
puffoagent agent create \
  --id helper \
  --url http://localhost:8065 \
  --token <bot_token> \
  --channels General,Random \
  --display-name "Helper"
```

The daemon spots the new directory within ~2s and starts the worker. No daemon restart needed.

`--channels` is informational — the bot actually talks in whatever channels its account has been added to on the server.

Optional flags: `--profile <path.md>` to seed a custom system prompt, `--provider anthropic|openai`, `--api-key`, `--model`, `--no-mention`, `--no-dm`.

## Adding a synced agent (easier)

If you've run `puffoagent login`, skip the CLI and use the webapp:

1. Click your profile avatar (top-right) → **My AI Agents**.
2. **+ New agent** — fill in display name, role, optional avatar + profile.
3. The webapp creates the bot, generates its token, adds it to the current team, and registers the AIAgent record with you as owner.
4. Within 30s your daemon pulls it down and starts the worker.

Pausing / archiving from the webapp flows back the same way.

## Archiving and export

```bash
puffoagent agent archive <id>        # stops + moves dir to ~/.puffoagent/archived/<id>-<timestamp>/
puffoagent agent export <id> out.zip # profile + memory + config
```

Archiving a synced agent only archives it locally — delete it from the webapp to remove it permanently.

## How state is stored

Everything lives under `~/.puffoagent/` (override with `PUFFOAGENT_HOME`):

```
~/.puffoagent/
  daemon.yml              # AI provider keys, optional server URL + user token
  daemon.pid              # daemon process id
  agents/
    <id>/
      agent.yml           # mm url + token, channels, state, triggers
      profile.md          # system prompt
      memory/             # per-agent memory + token_usage.json
      runtime.json        # live stats written by the worker
  archived/
    <id>-<timestamp>/
```

The CLI is file-driven. Creating an agent writes files; pausing flips a `state` field in `agent.yml`; the daemon's reconciler notices and acts within a few seconds. There is no IPC port.

In server-synced mode, the sync loop overwrites `agent.yml` + `profile.md` for every agent owned by the logged-in user, derives an ASCII-safe `<id>` from the bot username, and archives directories for agents that the server no longer reports.

## Stopping the daemon

`Ctrl+C` in the terminal running `puffoagent start`. Workers are cancelled cleanly before the process exits.

## Troubleshooting

- **"daemon: not running"**: start it with `puffoagent start` in another terminal.
- **Agent listed as `stale`**: worker's last runtime.json heartbeat is >30s old. Check the daemon's terminal log for errors.
- **`runtime: error`** in `list`: open `~/.puffoagent/agents/<id>/runtime.json` — the `error` field has the reason (usually a missing API key or bad bot token).
- **Synced agent stuck `offline`**: confirm `puffoagent login` worked (`puffoagent status` will show the server URL), then wait up to 30s for the next sync tick. The daemon log prints every sync attempt.
- **Windows $EDITOR default** is `notepad`. Set `$EDITOR` to override.
- **Two daemons at once**: refused via `daemon.pid` check. If the pid file is stale after a crash, delete `~/.puffoagent/daemon.pid` and start again.

## Running from a clone without installing

A thin shim at the repo root dispatches to the CLI without `pip install`:

```bash
git clone https://github.com/puffo-ai/puffoagent.git
cd puffoagent
python main.py start        # equivalent to `puffoagent start`
```

It inserts `./src` on `sys.path` and calls the same CLI module.
