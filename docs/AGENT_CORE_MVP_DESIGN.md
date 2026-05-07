# Local Agent Core MVP Design

This note captures the MVP design for rewriting the `cli-local` agent core.
It intentionally excludes Web UI implementation. The Web team only needs the
localhost API contract and status semantics; this runtime owns local detection,
device binding, provider process control, message handling, and core security
boundaries.

The important constraint is that Puffo cryptographic behavior must come from
the Rust foundation mounted as [`../agent-core/core`](../agent-core/core).
Node/TypeScript may
orchestrate processes and local APIs, but it should not reimplement message
encryption, signing, key storage, or certificate validation.

## Naming Rule

New runtime packages, binaries, and internal module names should not use a
`puffo-*` prefix. Use neutral names such as:

- `agent`;
- `agent-core`;
- `agent-native`;
- `core`.

This document still uses the product name "Puffo" for server/product concepts.
When referencing existing Rust crates under [`../agent-core/core`](../agent-core/core), prefer saying
"client crate" or "crypto crate" unless the exact Cargo package name is needed
for implementation.

## MVP Scope

The MVP should prove one thing:

```text
User installs one local package, connects this computer to Puffo, chooses
Claude Code or Codex, creates a local agent, and the agent can receive
authorized Puffo messages and send encrypted replies.
```

MVP includes:

- local daemon startup through one command;
- local environment and provider detection;
- device identity and account binding;
- local agent creation and lifecycle control;
- Claude Code and Codex CLI provider sessions;
- encrypted message receive/send through `core`;
- simple access modes: `safe`, `project`, `trusted`;
- logs and last-error diagnostics.

MVP excludes:

- Web UI implementation;
- automatic Claude/Codex installation;
- automatic provider login;
- Hermes/OpenClaw;
- fine-grained tool marketplace management;
- full cross-platform sandboxing;
- auto-update/uninstall flows, except where state layout should leave room.

## User Flows Supported By Agent Core

### 1. Install And Start Local Daemon

```text
Web page probes localhost
-> no daemon
-> user runs one command
-> daemon starts
-> daemon writes/loads local device state
-> daemon opens localhost API
-> user returns to Web and clicks Re-check
```

Example public command:

```bash
npm install -g @puffo-ai/agent-core && agent start
```

For a Web-distributed macOS bootstrap, host
`agent-core/scripts/bootstrap-macos.sh` and have the user run a single shell
command. The script checks for Node.js 20+; if a suitable Node is missing, it
downloads the official macOS Node.js 22.x tarball, verifies it against
`SHASUMS256.txt`, installs it into `$HOME/.agent-core/node`, installs the
configured `@puffo-ai/agent-core` package into the user-local npm prefix
`$HOME/.agent-core/npm`, and then execs that installed `agent` binary directly.
The hosted Web command can pass `bash -s -- --package <npm-spec>` after the
pipe, so a package rename or temporary source tarball does not require changing
the installed CLI binary name. The default package source is now the scoped
`@puffo-ai/agent-core` npm package, so the Web-hosted command does not need an
explicit package override once that package is published. `AGENT_CORE_INSTALL_PREFIX`,
`AGENT_CORE_NODE_DIR`, or their matching bootstrap flags can override those
user-local locations for testing.

The CLI bootstrap should only start and inspect the local runtime. It should
not silently install Claude, Codex, Git, or other third-party tools.

### 2. Detect Providers

After daemon startup, Web asks the local daemon for provider readiness.

The daemon detects:

- OS and architecture;
- Node/runtime version;
- macOS sandbox capability (`sandbox-exec`);
- `claude` path, version, and basic auth readiness;
- `codex` path, version, and basic auth readiness;
- Puffo server connectivity. The local package reports this as a generic
  `server` connectivity object and lets development builds override the target
  with `AGENT_CORE_SERVER_URL`;

The result should be structured and actionable:

```json
{
  "claude": {
    "installed": true,
    "ready": false,
    "reason": "not_logged_in",
    "fixCommand": "claude login"
  },
  "codex": {
    "installed": true,
    "ready": true,
    "version": "0.125.0"
  }
}
```

### 3. Bind This Computer

```text
Web asks daemon to start pairing
-> daemon creates/loads device identity
-> Puffo server creates pairing request
-> user confirms in Web
-> daemon/session becomes trusted for this account
```

Current local API rule: `POST /pairing/start` and
`GET /pairing/:pairingId` are public loopback routes so Web can start this flow
before it has a local grant. Browser callers must still come from a trusted
Origin before a confirmed poll can return a local grant. Direct
`POST /pairing/confirm` stays token-protected because it accepts the
server-issued native auth token.

Device identity and agent identity must stay separate:

- device identity represents this computer/runtime installation;
- agent identity represents a local agent running under this device/operator.

The Rust client/core layer should own signing, certs, key handles, and local key
storage. Node should receive opaque ids and high-level status only.

### 4. Create And Start Agent

The agent creation form only needs:

```text
Agent name
Provider: claude | codex
Access mode: safe | project | trusted
Instructions
Optional project path for project mode
```

The daemon then:

- creates an agent record and local workspace;
- binds the agent to the operator/device;
- saves runtime config;
- starts the selected provider session;
- starts the message loop for that agent.

### 5. Receive Messages And Decide Whether To Reply

The reply rule is:

```text
Within the agent's authorized visibility scope, the agent decides whether to
reply. If the message explicitly mentions the agent, or is a DM to the agent,
the agent must produce a visible result.
```

The runtime should not hard-code "only reply when @ mentioned". It should pass
clear metadata into the provider:

```json
{
  "mentioned": true,
  "dm": false,
  "mustRespond": true,
  "spaceId": "spc_...",
  "channelId": "chn_...",
  "threadRootId": "msg_...",
  "senderSlug": "sam",
  "body": "..."
}
```

The provider can return:

- `reply`;
- `silent`;
- `need_more_info`;
- `permission_denied`;
- `error`.

If `mustRespond = true`, `silent` is invalid. The runtime should ask the
provider for a status reply or send a structured failure/status message.

### 6. Manage Agent State

The local API should support:

- start;
- stop;
- restart;
- reset provider session;
- re-check provider readiness;
- read recent logs;
- read last error.

The MVP should favor understandable recovery over automatic repair.

## Proposed Agent Core File Organization

Target TypeScript/Node package:

```text
agent-core/
  package.json
  src/
    cli/
      index.ts
      start.ts
      stop.ts
      doctor.ts
      version.ts

    daemon/
      daemon.ts
      lifecycle.ts
      lockfile.ts
      supervisor.ts

    api/
      server.ts
      routes.health.ts
      routes.providers.ts
      routes.pairing.ts
      routes.agents.ts
      routes.logs.ts
      auth.ts
      errors.ts

    doctor/
      detector.ts
      os.ts
      node.ts
      claude-detector.ts
      codex-detector.ts
      network-detector.ts

    identity/
      device-identity.ts
      agent-identity.ts
      pairing.ts

    state/
      paths.ts
      store.ts
      device-store.ts
      agent-store.ts
      session-store.ts
      migrations.ts

    runtime/
      runtime-manager.ts
      agent-runtime.ts
      runtime-supervisor.ts
      agent-context.ts
      agent-input.ts
      agent-output.ts

    providers/
      provider-session.ts
      provider-types.ts
      process/
        child-process.ts
        process-env.ts
        stream-json.ts
      claude/
        claude-session.ts
        claude-command.ts
        claude-stream.ts
        claude-errors.ts
      codex/
        codex-session.ts
        codex-command.ts
        codex-stream.ts
        codex-errors.ts

    messaging/
      message-loop.ts
      message-router.ts
      message-filter.ts
      message-context.ts
      delivery.ts
      reply.ts

    policy/
      access-mode.ts
      policy.ts
      policy-resolver.ts
      workspace-policy.ts

    logs/
      logger.ts
      agent-logs.ts
      log-store.ts
      redact.ts

    platform/
      os.ts
      paths.ts
      shell.ts
      macos.ts
      windows.ts
      linux.ts

    native/
      core.ts
```

If the Rust binding lives outside this package, use a sibling crate/package:

```text
agent-core/
  crates/
    agent-native/
      Cargo.toml
      src/
        lib.rs
        client.rs
        identity.rs
        messages.rs
        store.rs
```

`agent-native` should wrap the existing `core/` submodule crates through N-API,
`napi-rs`, or another stable Node FFI boundary. It should expose use-case
methods, not primitive crypto methods.

Implementation note: the first cut uses a JSON CLI bridge in
`crates/agent-native/src/bin/agent-native-cli.rs`, called by
`src/native/cli-core.ts`. This keeps the TypeScript boundary the same while
avoiding premature N-API packaging. Once the native API settles, the CLI bridge
can be replaced by N-API without changing daemon/runtime callers.

## Core Rust API Integration

Use `agent-core/core` in this order of preference:

1. Prefer the client crate public SDK APIs.
2. Use the client crate providers/ports when building the native runtime.
3. Use the crypto crate `client_api` only inside the Rust native binding or
   provider implementation.
4. Do not call `crypto::base`, `crypto::service`, or primitive APIs from Node.

Relevant source files:

- [`../agent-core/core/crates/client/src/lib.rs`](../agent-core/core/crates/client/src/lib.rs)
- [`../agent-core/core/crates/client/src/api/sdk.rs`](../agent-core/core/crates/client/src/api/sdk.rs)
- [`../agent-core/core/crates/client/src/api/session.rs`](../agent-core/core/crates/client/src/api/session.rs)
- [`../agent-core/core/crates/client/src/api/command.rs`](../agent-core/core/crates/client/src/api/command.rs)
- [`../agent-core/core/crates/client/src/ports/crypto.rs`](../agent-core/core/crates/client/src/ports/crypto.rs)
- [`../agent-core/core/crates/crypto/src/client_api/mod.rs`](../agent-core/core/crates/crypto/src/client_api/mod.rs)
- [`../agent-core/core/docs/architecture/sdk-handoff.md`](../agent-core/core/docs/architecture/sdk-handoff.md)

### Current Core Entry Points

The client crate exposes:

```rust
pub use api::{ClientSdk, ClientSession};
pub use api::{Result, SdkError};
```

`ClientSdk` is the app-facing root:

- `ClientSdk::from_providers(config, crypto, server, store)`
- `ClientSdk::signup(...)`
- `ClientSdk::register_with_password(...)`
- `ClientSdk::login_with_password_restore_backup(...)`
- `ClientSdk::start_new_device_enrollment(...)`
- `ClientSdk::complete_new_device_enrollment(...)`
- `ClientSdk::open_session(...)`
- `ClientSdk::list_identities()`
- `ClientSdk::snapshot(slug)`

`ClientSession` is the active identity/session:

- `sync_once()`
- `process_pending_messages()`
- `send_channel_message(SendMessageCommand)`
- `send_direct_message(SendMessageCommand)`
- `send_channel_file(SendFileMessageCommand)`
- `send_direct_file(SendFileMessageCommand)`
- `queue_outbox(QueueOutboxCommand)`
- `snapshot()`

Message commands already model channel and direct routes:

```rust
SendMessageCommand::channel_text(space_id, channel_id, body)
SendMessageCommand::direct_text(recipient_slug, body)
  .with_thread(thread_root_id)
  .with_reply_to(reply_to_id)
```

The agent runtime should use these APIs for final Puffo replies instead of
manually creating envelopes in TypeScript.

### Crypto Boundary

The client SDK hides crypto behind `CryptoPort`:

```rust
fn seal_message(
    &self,
    sender_slug: &str,
    issued_at_ms: u64,
    command: &SendMessageCommand,
    recipient_devices: &[RecipientDeviceRecord],
) -> Result<SealedMessageRecord>;

fn open_message(
    &self,
    recipient_slug: &str,
    envelope: &MessageEnvelope,
    cert_cache: &CertificateCache,
    now_ms: u64,
) -> Result<Option<OpenedMessageRecord>>;
```

The message loop should call SDK/session use cases that eventually call these
methods. Node should not receive KEM keys, signing keys, plaintext key seeds,
or raw envelope crypto parameters.

The crypto crate `client_api` module is the app-facing crypto facade for Rust code. It
re-exports launch-shaped APIs such as:

- `Client`
- `Sealer`
- `Opener`
- `Signer`
- `Verifier`
- `Issuer`
- `DatabaseDek`
- `MessageEnvelopeContext`
- `MessageVerifyPolicy`
- `AuthenticatedHttpRequest`
- `AuthenticatedWsHandshake`
- Apple Keychain types behind the `apple-keychain` feature.

Do not expose these as a generic Node crypto toolkit. Wrap only the operations
the local agent runtime needs.

### macOS Key Storage

On macOS, the Rust provider can use:

```rust
client::providers::KeychainCryptoProvider::default_namespace()
client::providers::KeychainCryptoProvider::with_service(service)
client::providers::KeychainCryptoProvider::with_profile(profile)
```

Internally this uses:

```rust
crypto::client_api::Client::launch_keychain()
crypto::client_api::AppleKeychainStore
crypto::client_api::AppleKeychainProfile
```

Production Keychain policy from `core`:

- root signing key: user presence in production;
- device signing key: background-readable launch policy;
- device KEM key: background-readable launch policy;
- subkey signing key: background-readable launch policy.

The Node daemon should treat local key operations as opaque Rust calls. It may
show user-facing status such as `keychain_prompt_required`, `keychain_denied`,
or `identity_missing`, but it should never log or persist key material.

### Local Store

The Rust client already has store provider boundaries:

```rust
SdkStoreProvider::memory()
SdkStoreProvider::sqlite(SqliteStoreProvider::open_sqlcipher_with_dek(path, &database_dek)?)
```

`DatabaseDek` is crypto-owned local DB key material. The agent runtime does not
pass the raw DEK through TypeScript. Current production-profile sidecar behavior:

```text
Node asks Rust native binding to open the local product session/store.
Rust generates/restores the SQLCipher DEK from local macOS Keychain.
Rust opens the SQLCipher-backed store with that DEK.
Node receives an opaque session handle.
```

### Server And Realtime

The client crate has server ports for messages and realtime:

- `MessagesServerPort::pending_messages`
- `MessagesServerPort::post_message`
- `MessagesServerPort::ack_message`
- `RealtimeServerPort::connect`
- `RealtimeServerPort::poll_realtime`

For MVP, the local agent message loop can use polling or server-triggered
events. The important runtime contract is:

```text
Rust/client layer owns envelope open, cert refill, replay checks, local storage,
and encrypted send. Node owns scheduling, provider prompt construction, and
provider process IO.
```

## Suggested Native Boundary For Node

Expose opaque handles, not key bytes:

```ts
type CoreSessionHandle = string

interface CoreNative {
  openOrCreateDevice(input: OpenDeviceInput): Promise<DeviceStatus>
  startPairing(input: StartPairingInput): Promise<PairingStatus>
  confirmPairing(input: ConfirmPairingInput): Promise<DeviceStatus>

  openAgentSession(input: OpenAgentSessionInput): Promise<CoreSessionHandle>
  syncOnce(handle: CoreSessionHandle): Promise<SyncReport>
  processPendingMessages(handle: CoreSessionHandle): Promise<OpenedAgentMessage[]>

  sendChannelReply(handle: CoreSessionHandle, input: SendChannelReplyInput): Promise<MessageRef>
  sendDirectReply(handle: CoreSessionHandle, input: SendDirectReplyInput): Promise<MessageRef>

  snapshot(handle: CoreSessionHandle): Promise<SessionSnapshot>
  closeSession(handle: CoreSessionHandle): Promise<void>
}
```

The Rust binding may internally hold:

- `ClientSdk`;
- `ClientSession`;
- `SdkServerProvider`;
- `SdkStoreProvider`;
- `KeychainCryptoProvider` or a future platform provider.

Node should never receive:

- root/device/subkey private keys;
- KEM private keys;
- seed material;
- raw SQLCipher DEK;
- decrypted envelope internals beyond the message record fields required for
  provider prompt construction.

## Runtime Module Responsibilities

### `cli/`

Parses `agent start`, `stop`, `doctor`, and `version`. It should not
contain runtime business logic.
It also exposes `agent rotate-token`, which rotates the local control token
through the running daemon API when possible and directly updates local state
when the daemon is stopped.

### `daemon/`

Owns local process lifecycle:

- single-instance lock;
- startup/shutdown;
- local API server;
- runtime supervisor;
- resume agents persisted as `running` after ungraceful daemon exits;
- signal handling.

### `api/`

Localhost API consumed by Web. It should return statuses and invoke runtime
services, not spawn provider processes directly.

Discovery routes are public so Web can detect the daemon:

```text
GET /health
GET /providers
```

Management routes require the local control token generated in `device.json` or
a short-lived local management grant minted from that control token. The token
is accepted through either `Authorization: Bearer <token>` or
`X-Agent-Core-Token: <token>`. Grant-management routes accept the local control
token only, so a grant cannot mint or revoke other grants.
The local control token can rotate itself through a control-token-only route;
rotation clears existing local grants so Web sessions can be fully
disconnected locally.

Minimum API:

```text
GET  /health
GET  /providers
POST /pairing/start
POST /pairing/confirm
POST /local-grants
DELETE /local-grants/:id
POST /local-control-token/rotate
GET  /agents
POST /agents
GET  /agents/:id
POST /agents/:id/start
POST /agents/:id/stop
POST /agents/:id/restart
POST /agents/:id/reset-session
POST /agents/:id/policy
GET  /agents/:id/status
GET  /agents/:id/logs
GET  /diagnostics
```

Dev/mock-only API, disabled unless explicitly enabled:

```text
POST /agents/:id/dev-inject
```

This route exists only to exercise the local sidecar message loop before the
real product server pairing/transport contract is wired. It injects a dev
encrypted channel message and ticks the target agent once. It must stay disabled
in normal installs.

### `doctor/`

Detects environment readiness. It is allowed to run `which`, `--version`,
and small provider probes. It should return structured fix commands.

### `state/`

Owns local runtime files, not Puffo crypto state:

```text
~/.agent-core/
  daemon.json
  device.json
  agents/
    <agent-id>/
      agent.json
      runtime.json
      session.json
      policy.json
      workspace/
      logs/
```

Crypto state should be delegated to `core` store providers or platform secret
storage.

### `runtime/`

Owns agent lifecycle:

- create/start/stop/restart;
- reset provider session;
- prevent duplicate starts;
- coordinate message loop and provider session;
- update status, last active, and last error.

Current MVP implementation detail: if an agent has `coreIdentity`, startup opens
an opaque native core session handle and schedules a polling loop. A failed
native session open marks the agent start as failed instead of pretending the
agent is connected. The localhost API requires either `operatorSlug` or an
already-created `coreIdentity` for `start: true` creation and refuses `/start`
or `/restart` for persisted agents without `coreIdentity`, so Web cannot
accidentally start only the provider subprocess without attaching the product
message loop. Native creation uses `operatorSlug`; the faster MVP path can let
Web sign/register the agent association with its browser-held identity and then
attach `coreIdentity` metadata to the local daemon. That local handoff may omit
`coreIdentity.source` or send `web_signed`; `native` is reserved for identities
created by Rust core. Daemon startup applies the same rule to legacy persisted
`running` agents: if `coreIdentity` is missing, the agent is marked `error` and
not resumed.

### `providers/`

Owns CLI-specific behavior.

Shared interface:

```ts
interface ProviderSession {
  start(): Promise<void>
  stop(): Promise<void>
  send(input: AgentInput): Promise<AgentOutput>
  resetSession(): Promise<void>
  getStatus(): ProviderStatus
}
```

Claude-specific code stays under `providers/claude/`.
Codex-specific code stays under `providers/codex/`.

### `messaging/`

Owns the runtime loop:

```text
poll/realtime event
-> core processes pending encrypted envelopes
-> runtime filters visibility and self-messages
-> runtime builds provider input with mustRespond metadata
-> provider returns output
-> core sends encrypted reply when needed
```

Current MVP implementation detail: the loop can be driven by the daemon timer or
manually with `RuntimeManager.tickAgent(id)` in tests/recovery paths. It filters
self-messages by both the local agent UUID and the core agent slug because core
message sender ids are identity slugs, not local config ids.

### `policy/`

MVP access modes:

```text
safe:    provider cwd/home under agent workspace only
project: provider may access one selected project path
trusted: provider inherits user-like environment, except daemon-internal AGENT_CORE_* vars
```

This module should produce a `ResolvedPolicy` used by provider launch. Later it
can compile into a Rust sandbox launch policy without changing providers.

Current macOS implementation: `safe` and `project` provider processes are
wrapped with `sandbox-exec` by default using a generated profile. `safe` writes
only under agent home/workspace; `project` also writes the selected project
path. `trusted` skips the sandbox. `AGENT_CORE_SANDBOX=off` is a local
debugging opt-out only for unrestricted `safe`/`project` agents; restrictive
policy still forces sandboxing. `networkAccess: "deny"` is stored per agent and
omits network access from that agent's sandbox profile. `AGENT_CORE_NETWORK=off`
makes newly created agents default to `networkAccess: "deny"`. `deniedTools` is
stored per agent and adds executable names or absolute paths to that agent's
`process-exec` deny rules. The profile also denies execution of sensitive local
tools such as `security`, `lsof`, `ps`, `dtrace`, `fs_usage`, `scutil`,
`launchctl`, and `osascript`; append additional absolute executable paths with
`AGENT_CORE_DENIED_EXECUTABLES`.
`fileAccess.readablePaths` and `fileAccess.writablePaths` are the MVP file
resource control surface for Web. They accept extra absolute existing
directories for `safe`/`project` agents, canonicalize them with `realpath`, and
compile them into sandbox read/write roots. Writable roots are also readable.
`providerConfigPaths` is the separate provider-home projection surface for
MCP/commands/skills/config that providers expect under their `HOME`, for
example `.claude/commands` or `.codex/skills`. Entries are relative to the
user's real home, constrained to supported provider-owned config paths, copied
into the isolated agent home, and refused if any source or target component is
a symlink. Broad roots such as `.claude` or `.codex` are refused so caches,
history, and unrelated provider state are not silently mirrored. `trusted`
rejects these restrictive policy
fields because it does not run under the generated sandbox.
`POST /agents/:id/policy` can update these policy fields independently of
runtime state; if the target agent is running, the runtime restarts it so the
new sandbox/profile is applied.

For `safe` and `project`, provider CLIs receive a virtual `HOME`. To preserve
existing Claude/Codex login state without exposing the user's entire home
directory, the policy layer projects a narrow credential allowlist into the
agent home:

```text
codex:  .codex/auth.json, .codex/config.toml
claude: .claude.json, .claude/.credentials.json, .claude/settings.json
```

This projection can be disabled with `AGENT_CORE_CREDENTIALS=off`. It is a
provider credential bridge only; crypto identity and message keys still stay
inside the Rust `core` boundary.
Per-agent `providerConfigPaths` extend this projection for provider-owned
config directories without broadening the sandbox filesystem roots. They are
intended for explicit Web/user choices, not broad default home mirroring.
Projection is bounded by file count, per-file size, total bytes, and recursion
depth; oversized files are skipped instead of failing agent startup.

### `logs/`

Redacted local logs only. It must redact:

- tokens;
- authorization headers;
- provider API keys;
- Keychain errors containing account details;
- raw message bodies in high-level daemon logs unless user asks for provider
  trace logs.

Log directories are created with `0700` and log files with `0600`. Log tail
reads are bounded and redacted again before returning through the local Web API.
Provider transcript logs should be opt-in and scoped per agent.

## Current Core Gaps To Track

These are not blockers for the design, but they need explicit decisions before
implementation:

1. Agent identity creation now has a client-crate use-case entrypoint in the
   submodule: `ClientSession::create_agent_identity`. It creates an
   operator-bound agent identity and signs the operator attestation in Rust.
   Server-side registration/pairing still needs to persist and approve that
   binding.
2. The default native bridge is now a persistent Rust sidecar with a JSONL
   protocol. It keeps SDK/store/server state alive across daemon requests and
   has a dev-only injected-message test path. Production still needs real
   account-bound pairing/session verification through that sidecar or a future
   N-API binding.
3. The client crate has HTTP provider and port mappings, and the native sidecar
   has feature-gated production provider construction plus signed HTTP auth
   injection. Production still needs backend PR #25/#26 merge/deploy and
   end-to-end operator identity/session bootstrap verification.
4. The SDK API is synchronous Rust today. Node integration should run native
   calls off the event loop or expose async N-API wrappers.
5. The exact "opened message -> provider input" shape is now implemented at the
   local runtime boundary, but decrypted message/open verification must remain
   in Rust/core.
6. Sandbox enforcement is not part of `core` today. Treat it as a separate Rust
   native launcher later; do not mix it with crypto/client APIs.

The concrete production server contract still needed for device pairing, agent
identity registration, message transport, and local-Web authorization is tracked
in [`AGENT_CORE_SERVER_CONTRACT_NEEDED.md`](AGENT_CORE_SERVER_CONTRACT_NEEDED.md).
The focused backend/Web pairing handoff is tracked in
[`AGENT_CORE_PAIRING_CONTRACT.md`](AGENT_CORE_PAIRING_CONTRACT.md).

## Implementation Order

1. Define local daemon state layout and API DTOs.
2. Add a thin Rust native binding over the client crate for device/session/message
   operations.
3. Implement provider detection for Claude and Codex.
4. Implement Claude provider session.
5. Implement Codex provider session.
6. Implement runtime manager and state persistence.
7. Implement message loop using the native core binding.
8. Add access modes as launch policy inputs.
9. Add logs/diagnostics and common recovery paths.

The first end-to-end milestone should be:

```text
agent start
-> daemon health is visible
-> provider detector reports Claude/Codex
-> one local agent starts
-> one encrypted Puffo message is opened through core
-> provider generates a reply
-> reply is sent through core as an encrypted Puffo message
```
