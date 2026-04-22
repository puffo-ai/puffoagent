# Changelog

All notable changes to `puffoagent` are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.6.0] — 2026-04-22

Hermes becomes a first-class harness on `cli-docker`, plus two
agent-feedback fixes that sharpen how agents read multi-agent threads.

### Added
- **Hermes harness** as a second supported harness on `cli-docker`
  (alongside `claude-code`). Runs `hermes chat --provider anthropic
  --quiet [--continue] -q <prompt>` per turn, shares the host's
  `.credentials.json` for auth, and auto-registers the puffo MCP
  server inside the container so all 18 puffo tools (`send_message`,
  `get_channel_history`, etc.) are callable from a hermes turn.
  Docker image bumped `v6` → `v7`. Select per-agent with
  `puffoagent agent runtime <id> --harness hermes`. `cli-local` rejects
  `harness=hermes` for now — support needs separate design work.
- **`Harness` abstraction** decoupling the agent engine (what runs
  inside the runtime) from the runtime kind (where it runs). Claude-
  Code-specific MCP tools (`install_skill`, `refresh`, etc.) now
  return a clear error under non-claude harnesses rather than silently
  writing to paths hermes doesn't read.

### Fixed
- **`[SILENT]` reply suppression** is now a substring match, not
  equality. Hedged replies like `[SILENT] I wasn't mentioned in this
  thread` are properly suppressed instead of leaking to the channel as
  posts. The primer still asks for exactly `[SILENT]`; this just
  tolerates the prose agents sometimes wrap around it.
- **Self-mention rewrite.** Self-mentions are rewritten to
  `@you(<bot_username>)` in the message text (previously stripped
  outright), and the structured `mentions:` preamble now includes
  self with `is_self: true`. Two independent signals the agent was
  addressed, so multi-agent threads like `@agent2 please do X @agent1`
  no longer confuse agent1 about who was tagged.
- **Auto-archive agents** when their Puffo space is deleted via a new
  `delete_team` websocket handler; MCP HTTP helpers surface 4xx
  clearly so agents stop retrying posts to removed channels.
- **Concurrent docker-image builds** across workers are serialised so
  two agents booting on a cold host don't both race to build the same
  `puffo/agent-runtime:vN` tag.
- **Hermes integration fixes:**
  - Parser handles missing `session_id:` line and `--continue` resume
    marker shapes that hermes emits on fresh vs. resumed sessions.
  - Retries fresh (clearing the sentinel) when `--continue` rejects a
    stale resume with `No previous CLI session found to continue`.
  - Drains stderr and logs a reply preview so crashes / auth failures
    are visible in the daemon log.
  - Passes Claude Code's OAuth access token via `ANTHROPIC_API_KEY` on
    `docker exec` — hermes' auto-discovery was unreliable on fresh
    containers.
  - Dockerfile installs `hermes-agent` from git rather than PyPI
    (PyPI package was stale).

### Changed
- **Hermes turns run as one-shot subprocesses.** The initial approach
  of a long-lived stdio-pipe subprocess didn't fit hermes' interactive-
  mode contract (requires a real TTY, treats piped EOF as "user
  quit"). Each turn now spawns `hermes chat -q --continue` with
  hermes' own on-disk session store providing multi-turn continuity.

## [0.5.0] — 2026-04-21

### Added
- **Agent-scoped skills and MCP servers.** Agents extend their own
  toolkit at runtime via new MCP tools: `install_skill` /
  `uninstall_skill` / `list_skills` (writes
  `workspace/.claude/skills/<name>/SKILL.md`);
  `install_mcp_server` / `uninstall_mcp_server` / `list_mcp_servers`
  (writes `workspace/.mcp.json`); `refresh(model=None)` respawns the
  claude subprocess so new tools are discovered.
- **`puffoagent agent refresh-ping <id>`** diagnostic that dumps
  credential state and full one-shot output for reproducible
  troubleshooting.

### Fixed
- **Unified cli-local credentials.** Previously each cli-local agent
  copied `.credentials.json` once and diverged, stranding agents with
  dead refresh tokens. Now every cli-local agent symlinks at the
  host's live file (copy fallback on Windows without Developer Mode,
  re-synced on each `refresh_ping` tick). Matches `cli-docker`'s
  bind-mount semantics — one `claude login` heals everything.
- **Host sync** copies directory-form skills (old code copied flat
  `*.md` files that Claude Code doesn't load as skills).
- **Runtime hardening** from the 2026-04-21 Core 3 freeze sprint:
  `ClaudeSession` stream reader limit widened; mid-turn death now
  recovers by killing the subprocess and returning silent rather than
  wedging; real inference smoke test replaces trusting
  `claude auth status`; auth failures propagate to a visible
  `auth_healthy` flag.

### Changed
- **Refresh threshold** dropped 15 min → 5 min to land inside
  Anthropic's OAuth accept window for refresh (they refuse to rotate
  a token more than ~10 min from expiry).
- **`PUFFO_RUNTIME_KIND`** env var threads adapter kind through to the
  MCP server so `install_mcp_server` can skip the host-local-command
  check on cli-local where paths resolve.
- **Dockerfile** gained `uv` for uvx-launched MCP servers; image
  bumped `v5` → `v6`.

## [0.4.0]

Permission proxy modes, refresh mutex, device-code login, `whoami` +
thread-root MCP fixes.

## [0.3.1]

Two-layer `CLAUDE.md` + `reload_system_prompt` tool + `rename` and
avatar CLI commands.

## [0.3.0]

Per-agent isolation, shared filesystem at `/workspace/.shared`, richer
agent context in the system prompt, eager spawn on daemon start.

## [0.1.1]

`pyproject` metadata fix + matching wheel URL in README.

## [0.1.0]

Initial public release. CI smoke tests + release workflow.

[0.6.0]: https://github.com/puffo-ai/puffoagent/releases/tag/v0.6.0
[0.5.0]: https://github.com/puffo-ai/puffoagent/releases/tag/v0.5.0
[0.4.0]: https://github.com/puffo-ai/puffoagent/releases/tag/v0.4.0
[0.3.1]: https://github.com/puffo-ai/puffoagent/releases/tag/v0.3.1
[0.3.0]: https://github.com/puffo-ai/puffoagent/releases/tag/v0.3.0
[0.1.1]: https://github.com/puffo-ai/puffoagent/releases/tag/v0.1.1
[0.1.0]: https://github.com/puffo-ai/puffoagent/releases/tag/v0.1.0
