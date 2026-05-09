# cli-local Agent Runtime Notes

This note records the code paths that matter for the current
`runtime.kind: cli-local` agent runtime. It is architecture-focused:
use it to understand what must be preserved or split out when moving
the local agent runtime toward Node + Rust or adding a macOS sandbox.

Status: legacy Python reference. The rewritten MVP implementation lives under
[`../agent-core`](../agent-core), publishes as `@puffo-ai/agent-core`, installs
binary `agent`, and keeps new runtime/binary names free of a `puffo-*` prefix.

## Summary

`cli-local` runs Claude Code as a host subprocess. It gives the
subprocess a per-agent virtual home and workspace, wires in the Puffo
MCP server, and keeps a long-lived Claude stream-json session alive
across turns.

It is a convenience runtime, not a hard security boundary:

- the subprocess runs as the same OS user as `puffo-agent`;
- `HOME` and `cwd` provide soft namespace separation only;
- host Claude credentials, skills, and MCP registrations are synced or
  linked into the per-agent home;
- no OS sandbox is applied today.

## Runtime Topology

```text
puffo-agent daemon
  -> Worker
     -> PuffoAgent shell
        -> LocalCLIAdapter
           -> ClaudeSession
              -> claude subprocess
                 -> Puffo MCP stdio server
     -> PuffoCoreMessageClient
        -> WebSocket receive/decrypt/store
        -> encrypted outbound post fallback
```

Inbound messages arrive through `PuffoCoreMessageClient`, are
decrypted and stored in `messages.db`, then passed to
`PuffoAgent.handle_message()`. The agent delegates one turn to
`LocalCLIAdapter`, which sends the latest user message into the
long-lived Claude Code stream-json session.

Outbound replies usually go through the MCP `send_message` tool. If
Claude does not call `send_message` and does not mark the turn silent,
`PuffoAgent` posts a fallback reply through `PuffoCoreMessageClient`.

## Core cli-local Files

These files are the narrowest implementation surface for `cli-local`.

| File | Role |
| --- | --- |
| `src/puffo_agent/agent/adapters/local_cli.py` | Builds and owns the host Claude Code adapter. Sets per-agent `HOME`, writes Claude MCP config, registers permission hooks, syncs host Claude config/skills/MCP/credentials, and constructs `ClaudeSession`. |
| `src/puffo_agent/agent/adapters/cli_session.py` | Implements the long-lived Claude Code stream-json protocol. Handles subprocess lifecycle, `--resume`, `cli_session.json`, event parsing, auth-error retry, tool-call metadata, and audit logging. |
| `src/puffo_agent/agent/adapters/base.py` | Defines the adapter contract: `Adapter`, `TurnContext`, `TurnResult`, `warm`, `reload`, and OAuth refresh orchestration. |

Approximate current size:

```text
local_cli.py       626 LOC
cli_session.py     710 LOC
base.py            302 LOC
```

## Runtime Selection and Config Files

These files select `cli-local`, validate runtime combinations, or
define on-disk state used by `cli-local`.

| File | Role |
| --- | --- |
| `src/puffo_agent/portal/worker.py` | `build_adapter()` chooses `LocalCLIAdapter` when `runtime.kind == "cli-local"` and injects Puffo MCP env. The worker also builds `CLAUDE.md`, warms adapters, handles reload/refresh flags, and serializes message turns. |
| `src/puffo_agent/portal/state.py` | Owns the `~/.puffo-agent` layout and helper functions used by `cli-local`: `agent_home_dir`, `cli_session_json_path`, `seed_claude_home`, `link_host_credentials`, `_sync_credentials_from_keychain`, `sync_host_skills`, `sync_host_mcp_servers`, and `RuntimeConfig.permission_mode`. |
| `src/puffo_agent/portal/runtime_matrix.py` | Defines valid runtime/provider/harness triples. `cli-local` is a harness-bearing runtime, but the current local adapter only accepts `claude-code`. |
| `src/puffo_agent/portal/cli.py` | CLI commands expose runtime selection, status, and refresh-ping helpers. |

## Harness Files

`cli-local` currently only supports the Claude Code harness, but the
matrix already models other harnesses.

| File | Role |
| --- | --- |
| `src/puffo_agent/agent/harness/base.py` | Harness interface and capability comments. |
| `src/puffo_agent/agent/harness/claude_code.py` | Declares the Claude Code harness. |
| `src/puffo_agent/agent/harness/__init__.py` | Resolves `runtime.harness` strings into harness objects. |
| `src/puffo_agent/agent/harness/hermes.py` | Declared but rejected by `cli-local`; implemented only in the Docker path today. |
| `src/puffo_agent/agent/harness/gemini_cli.py` | Reserved/declarative harness. |

## MCP and Tooling Files

Claude Code gets Puffo capabilities through a per-agent MCP config.

| File | Role |
| --- | --- |
| `src/puffo_agent/mcp/config.py` | Builds the MCP config file and `PUFFO_CORE_*` environment for the stdio server. |
| `src/puffo_agent/mcp/puffo_core_server.py` | MCP stdio entry point. Registers Puffo core tools plus local host/project tools. |
| `src/puffo_agent/mcp/puffo_core_tools.py` | Implements Puffo message, history, channel, file upload, and identity tools over signed/encrypted Puffo API calls. |
| `src/puffo_agent/mcp/host_tools.py` | Implements host/project tools such as skill install, MCP server install, refresh, reload, and listing. Some tools are Claude Code specific. |
| `src/puffo_agent/mcp/data_client.py` | Read-only client for the daemon data service, used so MCP subprocesses do not open the agent SQLite database directly. |
| `src/puffo_agent/hooks/permission.py` | Claude Code `PreToolUse` hook. This is legacy permission-proxy plumbing; current `cli-local` only supports `bypassPermissions` in practice. |

## Message and Prompt Support

These files are not specific to `cli-local`, but `cli-local` depends
on them for the end-to-end runtime flow.

| File | Role |
| --- | --- |
| `src/puffo_agent/agent/core.py` | Per-agent shell. Formats inbound messages into structured model context, delegates a turn to the adapter, and decides MCP-only vs fallback replies. |
| `src/puffo_agent/agent/puffo_core_client.py` | WebSocket bridge for Puffo messages: receive, decrypt, verify, store, prioritize, and post encrypted replies. |
| `src/puffo_agent/agent/message_store.py` | Local SQLite message store. |
| `src/puffo_agent/agent/shared_content.py` | Shared primer, managed `CLAUDE.md` assembly, memory snapshot injection, and CLI-runtime instructions. |
| `src/puffo_agent/portal/data_service.py` | Loopback read-only service used by MCP subprocesses for message history and channel-space lookup. |

## Test Files

The main tests covering `cli-local` behavior are:

| File | Coverage |
| --- | --- |
| `tests/test_cli_session_recovery.py` | Claude stream-json session, resume failure recovery, auth failure handling. |
| `tests/test_permission_mode.py` | `permission_mode`, command construction, and hook settings wiring. |
| `tests/test_host_credentials.py` | Host Claude credential linking/copying. |
| `tests/test_host_sync.py` | Host skills/MCP sync and `LocalCLIAdapter._verify()` integration. |
| `tests/test_harness.py` | Harness selection and `cli-local` rejection of Hermes/Gemini. |
| `tests/test_worker_integration.py` | Worker-to-MCP env construction, including `runtime_kind="cli-local"`. |
| `tests/test_hook_permission.py` | PreToolUse permission hook behavior. |
| `tests/test_puffo_core_tools.py` | Puffo MCP tools used by CLI runtimes. |

## Current Security Properties

`cli-local` should be treated as trusted-host mode.

Current boundaries:

- per-agent `HOME`;
- per-agent workspace;
- separate Claude session id per agent;
- per-agent Puffo keystore and `messages.db`;
- MCP tools run under the agent identity.

Missing boundaries:

- no OS sandbox;
- no file-system allowlist;
- no network allowlist;
- no Keychain isolation;
- host Claude skills/MCP registrations are synced into the agent home;
- process environment inherits from the daemon process.

For a sandboxed local runtime, do not put policy enforcement directly
inside `LocalCLIAdapter`. Add a separate launcher layer:

```text
LocalCLIAdapter / future CodexAdapter
  -> ProcessLauncher
     -> puffo-sandbox-run --spec <sandbox.json> --
        -> claude / codex
```

The adapter should keep owning provider protocol details. The launcher
should own process environment, cwd, sandbox wrapping, and process-tree
cleanup. Policy should be compiled before launch from an agent-level
profile, not hard-coded in the provider adapter.

## Suggested Refactor Boundaries

If extracting this to Node + Rust, avoid a file-for-file port. Preserve
the behaviors, but split the responsibilities:

```text
RuntimeManager
  start/stop/restart/warm per-agent runtimes

ProviderSession
  Claude stream-json session
  future Codex JSON/session handling

ProcessLauncher
  env/cwd/process lifecycle
  wraps commands with puffo-sandbox-run

PolicyProfile + SandboxSpec
  file/network/tool/auth mode

CredentialStrategy
  convenience vs strict provider auth

MCPConfigBuilder
  provider-specific MCP config generation

Gateway/DataClient
  Puffo messages, memory, files, audit, and local read-only data
```

Rust should own crypto, keystore, signed HTTP/WS, policy verification,
and the sandbox launcher. Node can own UI/control API, provider
bridges, MCP config orchestration, and process protocol parsing.
