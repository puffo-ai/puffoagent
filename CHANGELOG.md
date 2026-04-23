# Changelog

All notable changes to `puffoagent` are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.7.1] — 2026-04-23

### Fixed
- **Double-post when the agent uses `send_message`.** MCP posts
  immediately from inside the turn, but the adapter was also
  collecting every surrounding text block ("Let me read the
  file...", "Replied in thread.") into `TurnResult.reply`, which
  the shell then posted as a second message in the same slot.

  Both `cli_session` and `sdk` adapters now record each
  `mcp__puffo__send_message` call's `(channel, root_id)` pair in
  `TurnResult.metadata["send_message_targets"]`.
  `PuffoAgent.handle_message` suppresses its auto-reply iff at
  least one of those targets matches the current turn's slot —
  the same `(channel_id, root_id)` the worker would post the
  auto-reply to (see `portal/worker.py`, `post_message(channel_id,
  reply, root_id=root_id)`).

  Matching is precise, not a blanket "send_message used → suppress":

  | Incoming `root_id` | `send_message` `root_id` | Same channel | Suppress? |
  |---|---|---|---|
  | `""` (top-level) | `""` | yes | **yes** — duplicate top-level posts |
  | `"thread-abc"` | `"thread-abc"` | yes | **yes** — duplicate thread replies |
  | `""` (top-level) | `"thread-abc"` | yes | no — different slots |
  | `"thread-abc"` | `""` | yes | no — different slots |
  | any | any | no | no — different channels |

  Channel matching accepts either a channel_id or channel_name in
  the tool's `channel` arg (MCP takes either form). Suppressed
  replies are still appended to `agent.log` so future turns see
  the narration as context; only the outbound post is skipped.

  Known narrow limitation: DMs addressed via `@handle` form in
  the `channel` arg won't match Mattermost's internal
  `user1__user2` channel_name. Agents that DM via channel_id are
  unaffected.

## [0.7.0] — 2026-04-23

Runtime management is now standardized around a 3D type system:
(**runtime**, **provider**, **harness**). Breaking change to
`runtime.kind` values in `agent.yml`, with a one-release migration
shim.

### Breaking changes
- **`runtime.kind` rename:** `chat-only` → `chat-local`, `sdk` →
  `sdk-local`. `cli-local` and `cli-docker` are unchanged. Existing
  `agent.yml` files with the old values are auto-migrated on load
  with a one-line WARNING in the daemon log; operators should
  update by running `puffoagent agent runtime <id> --kind chat-local`
  (or `sdk-local`) once to persist the new spelling. The shim stays
  through 0.7.x and is removed in 0.8.
- **Invalid (runtime, provider, harness) triples now fail fast at
  load** rather than silently misbehaving. Examples rejected:
  `harness=gemini-cli` without `provider=google`; `harness=claude-code`
  with `provider=openai`; `kind=cli-sandbox` (reserved, not yet
  implemented).

### Added
- **`portal/runtime_matrix.py`** — single source of truth for the
  (runtime, provider, harness) validity matrix. Constants,
  `validate_triple()`, `migrate_legacy_kind()`, and default-resolver
  helpers are all co-located here so adapters, the CLI, and tests
  share one definition.
- **`provider` as first-class** across every runtime kind, not just
  `chat-local`. Declared values: `anthropic` / `openai` / `google`.
  `puffoagent agent runtime --provider` now takes a `choices=`
  constrained list.
- **`gemini-cli` harness on `cli-docker`.** Ships Google's
  `@google/gemini-cli@0.38.2` inside the puffo-agent-runtime image
  and drives it via `gemini -p <prompt> --output-format json
  [-r latest]` per turn. Auth is a static `GEMINI_API_KEY` from
  the daemon's `google.api_key` (set via `puffoagent init` or by
  editing `daemon.yml`). Session continuity uses gemini's built-in
  `-r latest` resume with our existing `cli_session.json` sentinel;
  stale-session retry falls back to a fresh start. `cli-local`
  rejects `harness=gemini-cli` with the same operator-sessions-
  collision reason as hermes — use `cli-docker` for now.
- **Gemini workspace provisioning** mirrors the Claude Code path,
  with one scope-split that's load-bearing for tool access:
  - `GEMINI.md` at `<agent_home>/.gemini/GEMINI.md` (**user scope**)
    written by the worker with the assembled primer + profile +
    memory snapshot. Gemini auto-discovers it via
    `$HOME/.gemini/GEMINI.md` every turn — no first-turn prompt-
    stitching needed. Operators can still own a project-level
    `<workspace>/GEMINI.md` for per-workspace overrides; gemini
    concatenates hierarchically.
  - Host skill sync: `sync_host_gemini_skills(host_home,
    project_dir)` copies `~/.gemini/skills/<name>/` →
    `<project_dir>/.gemini/skills/<name>/` (**project scope**).
    Caller passes `workspace_dir` as `project_dir` — gemini's
    skill resolver keys off cwd.
  - Host MCP sync: `sync_host_gemini_mcp_servers(host_home,
    project_dir, extra_servers={"puffo": ...})` writes to
    `<workspace>/.gemini/settings.json` (**project scope** — see
    below). Merges operator's host-level MCPs and injects the puffo
    MCP entry in a single write, preserving every non-`mcpServers`
    key. No separate `gemini mcp add` subprocess to race with.
  - Scope split rationale: gemini-cli 0.38.2's MCP resolver
    **defaults to project scope** (`<cwd>/.gemini/settings.json`),
    not user scope. An entry at `~/.gemini/settings.json` is
    silently ignored by `gemini mcp list` and never reaches the
    model — verified empirically by letting gemini write its own
    MCP via `gemini mcp add` and observing it land in `<cwd>`.
    GEMINI.md is still at user scope because its resolver DOES
    merge hierarchically.
  - New bind-mount: `<agent_home>/.gemini` → `/home/agent/.gemini`
    (always mounted regardless of harness, so the user-scope
    GEMINI.md and gemini's internal bookkeeping —
    `installation_id`, `history/`, `tmp/` — survive container
    restart). Project-scope `.gemini/` rides along inside the
    existing `<workspace>:/workspace` mount.
- **`DaemonConfig.google`** — new provider block (`api_key` +
  `model`) alongside anthropic and openai. `puffoagent init`
  prompts for a Google API key (env hint: `GEMINI_API_KEY` /
  `GOOGLE_API_KEY`). Required when any agent uses
  `harness=gemini-cli`; worker raises at adapter construction if
  it's missing rather than failing silently mid-turn.
- **Docker image bumped `v8` → `v9`** to pick up the gemini-cli
  install.
- **`cli-sandbox` runtime** declared as a reserved enum value. Load
  surfaces a clean "not yet implemented" error — namespace reserved
  for a future host-sandbox adapter (e.g. seatbelt / landlock).
- **`Harness.supported_providers()`** — each harness class declares
  its compatible providers so the matrix validator can reject
  mismatched triples deterministically.
- **`tests/test_runtime_matrix.py`** — 41 new tests covering every
  documented combo (positive + negative), legacy migration, and
  matrix invariants (e.g. every default harness-for-provider is a
  valid triple).
- **Collision-avoiding `_derive_agent_id` in `portal/sync.py`** —
  when two server-side agents have the same ASCII slug (e.g.
  `d2d2迷你` and `d2d2留声机` both → `d2d2`), the *oldest-created*
  keeps the plain slug and later conflicts get a
  `<base>-<user_id[:7]>` suffix. No migration needed: re-syncing
  agents are identified by `mattermost.bot_token` and keep their
  plain name regardless of which daemon version created them.
  Remotes are sorted by `create_at` before derivation for
  deterministic ordering across machines.
- **`puffoagent agent list` DISPLAY column** — shows each agent's
  display_name alongside the ID, readable at a glance even when
  the ID is an opaque user-id suffix or hash-form.
- **`tests/test_sync_id_collision.py`** — 14 new tests covering the
  collision rules (oldest-wins, three-way conflict, re-sync
  preservation, 64-char base truncation, stability across passes).

### Fixed
- **Gemini argv bug** — our turn preamble lines all begin with
  `- ` (markdown list syntax), and `gemini -p <value>` was handing
  yargs a separate argv that starts with `-`. yargs rejected with
  "Not enough arguments following: p" and fell through to printing
  its `Usage: gemini [options]` banner — which got leaked verbatim
  into Mattermost as the agent's reply (rc=0, empty parser-valid
  stdout). Fix: pass the prompt as a single argv token in
  `--prompt=<value>` form. Extracted `_build_gemini_argv` as a pure
  function so the invariant is testable; regression test locks
  in dash-leading + multi-line CJK preamble values.
- **Gemini MCP scope** — `sync_host_gemini_mcp_servers` now writes
  to `<workspace>/.gemini/settings.json` (project scope). The
  previous user-scope target was silently ignored by gemini's MCP
  resolver, so the puffo tool surface (`get_channel_history`,
  `send_message`, etc.) never reached the model.
- **Gemini JSON parser** — `session_id` extraction was looking
  inside `stats`; gemini 0.38.2 puts it at the top level. Error
  field is a dict (`{"type", "message", "code"}`), not a string —
  parser now unpacks `message` rather than stringifying the dict.
  Added `Usage: gemini` banner detection: if upstream prints help
  to stdout instead of JSON, return empty reply + clear error
  rather than leaking the banner to the channel.
- **Pre-exec log** for every gemini turn (argv with API key
  redacted) so failed turns are reproducible from the daemon log
  without reattaching the operator.

### Changed
- **`RuntimeConfig.kind` default** `chat-only` → `chat-local`.
- **`build_adapter`** dispatches on the new kind names; the old
  names never reach it (translated by `AgentConfig.load`).
- **`puffoagent agent runtime`** display reorders the fields to
  `kind / provider / harness / ...` matching the 3D framing.
  `--kind` and `--provider` now use `choices=` so invalid values
  are rejected at argparse time.
- **`puffoagent init`** help text and `agent create --runtime`
  default values updated to the new spellings.
- **`puffo_tools.py` MCP server** accepts new runtime-kind / harness
  values on its argv (`sdk-local`, `gemini-cli`) for parity.
- **DESIGN.md** reorganized around the 3D matrix — new compatibility
  table, validation section, and a "breaking changes in 0.7.0"
  pointer in the historical rollout list.
- **README.md** runtime-kinds table + YAML example updated to reflect
  the new names and the new `provider:` field.
- **`config.example.yml`** `default_provider` comment extended to
  list `google` as a supported value.

### Removed
- Internal `_HARNESS_*` enum string literals duplicated across code —
  now imported from `runtime_matrix`.

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

[0.7.0]: https://github.com/puffo-ai/puffoagent/releases/tag/v0.7.0
[0.6.1]: https://github.com/puffo-ai/puffoagent/releases/tag/v0.6.1
[0.6.0]: https://github.com/puffo-ai/puffoagent/releases/tag/v0.6.0
[0.5.0]: https://github.com/puffo-ai/puffoagent/releases/tag/v0.5.0
[0.4.0]: https://github.com/puffo-ai/puffoagent/releases/tag/v0.4.0
[0.3.1]: https://github.com/puffo-ai/puffoagent/releases/tag/v0.3.1
[0.3.0]: https://github.com/puffo-ai/puffoagent/releases/tag/v0.3.0
[0.1.1]: https://github.com/puffo-ai/puffoagent/releases/tag/v0.1.1
[0.1.0]: https://github.com/puffo-ai/puffoagent/releases/tag/v0.1.0
