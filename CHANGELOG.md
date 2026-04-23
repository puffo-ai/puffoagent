# Changelog

All notable changes to `puffoagent` are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.6.1] — 2026-04-22

Triage sweep from a post-0.6.0 agent code review. Correctness + hygiene
fixes, no breaking changes.

### Added
- **`runtime.max_turns`** field on `RuntimeConfig` with matching
  `puffoagent agent runtime --max-turns N` CLI flag. Caps the SDK
  adapter's agentic-loop iterations per conversation turn; previously
  hard-coded at 10. Default stays 10; only wired into the SDK adapter
  (CLI adapters delegate turn-bounding to the `claude` CLI).
- **`whoami`** is now in `PUFFO_TOOL_NAMES` so SDK-runtime agents get
  it in the auto-allowlist. The tool was always registered with
  `@mcp.tool()` but missing from the allowlist — CLI runtimes masked
  the omission because they run under `--dangerously-skip-permissions`.

### Fixed
- **`is_daemon_alive()` PID-reuse false positive.** Previously a raw
  `os.kill(pid, 0)` liveness check — after a reboot the same numeric
  PID could belong to any unrelated process, blocking a new daemon
  start. Now uses `psutil.Process(pid).cmdline()` to verify the
  process is actually `puffoagent ... start`. Matches four invocation
  shapes (bare, venv shim, `.exe`, `python -m`).
- **`_resolve_mentions` N+1 → batch.** `N` @-mentions in a message
  used to fan out into `N` individual `GET /users/username/<name>`
  requests. Now one `POST /users/usernames` with the full deduped
  list. Also skips the round-trip entirely when a message has no
  mentions.
- **cli-local startup banner** is now mode-aware. Under `default` /
  `acceptEdits` it logs INFO describing the permission proxy; only
  under `auto` / `dontAsk` / `bypassPermissions` does it keep the
  loud WARNING about unsandboxed host access. Previously every
  cli-local agent got the scary message regardless of whether the
  permission proxy was active.
- **Pinned claude-code CLI** in the `cli-docker` Dockerfile to
  `@anthropic-ai/claude-code@2.1.117`. Floating was a reproducibility
  hazard — each rebuild could pick up a stream-json or
  `--permission-mode` shift under us. Image bumped `v7` → `v8`.
- **`datetime.utcnow()`** replaced with `datetime.now(timezone.utc)`
  in `memory.py` (deprecated since Python 3.12). Memory-frontmatter
  timestamps now render with a clean `Z` suffix.
- **`_url_matches` docstring** no longer claims "scheme-tolerant"
  equality — the implementation lower-cases and strips trailing
  slashes only, and scheme is a real identity signal we shouldn't
  collapse.
- **`requirements.txt`** resynced with `pyproject.toml`: `mcp>=1.0`
  and `psutil>=5.9` added, files now track the same dependency list
  verbatim (in the same alphabetical order so diffs stay boring).

### Changed
- **Dependencies:** added `psutil>=5.9` to the runtime deps for the
  PID-identity check above.
- **Docstring / comment cleanups** across the repo: stale "previously
  we did X" framing dropped in favor of forward-looking wording;
  `docker_cli.py` bind-mount count corrected (five → six); `DESIGN.md`
  adapter table now lists all four kinds and reflects the shipped
  permission proxy + harness abstraction; `config.example.yml`
  rewritten from the legacy single-agent flat shape to the current
  `DaemonConfig` schema.
- **`_ms_to_iso`** deduplicated — extracted into
  `puffoagent/agent/_time.py` and imported by both `core.py` and
  `mattermost_client.py`.

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

[0.6.1]: https://github.com/puffo-ai/puffoagent/releases/tag/v0.6.1
[0.6.0]: https://github.com/puffo-ai/puffoagent/releases/tag/v0.6.0
[0.5.0]: https://github.com/puffo-ai/puffoagent/releases/tag/v0.5.0
[0.4.0]: https://github.com/puffo-ai/puffoagent/releases/tag/v0.4.0
[0.3.1]: https://github.com/puffo-ai/puffoagent/releases/tag/v0.3.1
[0.3.0]: https://github.com/puffo-ai/puffoagent/releases/tag/v0.3.0
[0.1.1]: https://github.com/puffo-ai/puffoagent/releases/tag/v0.1.1
[0.1.0]: https://github.com/puffo-ai/puffoagent/releases/tag/v0.1.0
