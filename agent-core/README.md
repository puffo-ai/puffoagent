# agent-core

Node/TypeScript implementation of the local agent core MVP.

This package intentionally uses neutral runtime names (`agent`, `agent-core`,
`agent-native`) and does not introduce new `puffo-*` runtime or binary names.
The npm package is published under the product org scope as
`@puffo-ai/agent-core`, while the installed CLI binary remains `agent`.
Product crypto and client behavior should come from the Rust `core/` submodule
workspace through the native boundary in `src/native/core.ts`.

## Install And Start

For a published package on macOS:

```bash
npm install -g @puffo-ai/agent-core && agent start
```

Release naming note: the unscoped `agent-core` name is already present on
npmjs as a security holding package (`0.0.1-security`), so the package uses
the available `@puffo-ai/agent-core` org scope. The installed CLI binary still
remains `agent`. The package publish config sets `access=public` for that
scoped release.

For Web-distributed one-command bootstrap, host
`scripts/bootstrap-macos.sh` from the Web app or docs site and ask the user to
run:

```bash
curl -fsSL https://example.test/agent-core/bootstrap-macos.sh | bash
```

The bootstrap script is not shipped inside the npm package. The npm package
contains only runtime files; Web owns hosting the latest bootstrap shell script
that chooses the package source and starts the installed `agent` binary.

The bootstrap checks for Node.js 20+. If a suitable Node is missing, it
downloads the official macOS Node.js 22.x tarball, verifies it against
`SHASUMS256.txt`, installs it into `$HOME/.agent-core/node`, installs
`@puffo-ai/agent-core` into `$HOME/.agent-core/npm`, and starts the installed
`agent` binary directly. Set `AGENT_CORE_INSTALL_PREFIX` to override the
user-local npm prefix and `AGENT_CORE_NODE_DIR` to override the user-local Node
directory while testing. Set `AGENT_CORE_NODE_MAJOR` to choose a newer Node.js
major for the fallback download; the value must be an integer and at least 20.
Test builds can override the Node.js distribution base with
`AGENT_CORE_NODE_DIST_BASE`, but the value must be an `https://` URL without whitespace. Those
override paths must be absolute user-owned directories;
the bootstrap refuses root, `$HOME`, `$HOME/.agent-core`, and common system
directories before it creates or replaces anything. It also refuses symlinked
path components and `..` traversal for the install prefix, the user-local Node
directory, and the temporary download directory. Node.js checksum and tarball
downloads are made with `curl` restricted to HTTPS, including redirects. The
script runs with `umask 077`, so user-local install files and fallback Node.js
files are private to the current user by default. It re-checks install and
download paths after creating parent directories and before replacing the
fallback Node.js directory. After npm install, it starts only the `agent` binary
from the selected user-local npm prefix and refuses to fall back to a different
`agent` found on `PATH`. Temporary Node.js download directories are removed on
bootstrap exit, including failed downloads.
Override the package source while testing unpublished builds:

```bash
bash scripts/bootstrap-macos.sh \
  --package 'git+https://github.com/<org>/<repo>.git#subdirectory=agent-core' \
  --run-scripts
```

`AGENT_CORE_PACKAGE` must be a single npm package spec without whitespace and
must not start with `-`; the `--package` option follows the same rule. The
default package source is `@puffo-ai/agent-core`. It installs with
`npm --ignore-scripts` by default. Use `--run-scripts` or set
`AGENT_CORE_NPM_RUN_SCRIPTS=1` only for trusted source or git package overrides
that need npm lifecycle scripts during testing.

It intentionally does not install Claude, Codex, Git, or provider credentials.
The daemon prints a localhost URL and local control token, then the user should
return to Web and click Re-check.

## Core Submodule

`core/` is expected to be a git submodule pointing at
`https://github.com/puffo-ai/core.git`. The native Rust crate depends on it with
local Cargo paths:

```text
crates/agent-native -> ../../core/crates/client
```

While the required Rust client changes are still waiting to land upstream, run
`npm run check:core-patch` from this package to verify that
`../docs/patches/agent-core-core-upstream.patch` still matches the submodule
diff from the recorded base in
`../docs/patches/agent-core-core-upstream.base` and applies cleanly to a
temporary clean worktree at that base. After changing `core/`, run
`npm run export:core-patch` to regenerate that handoff patch from the same
base and immediately re-run the verifier. The current upstream review
is https://github.com/puffo-ai/core/pull/18.

## Native Bridge

The default development bridge is a persistent Rust sidecar:

```bash
cargo run --manifest-path crates/agent-native/Cargo.toml --features dev-tools --bin agent-native-sidecar
```

Node calls this through `src/native/sidecar-core.ts`. The daemon keeps the
sidecar alive for its lifetime, so SDK/store/server state survives across native
requests. The sidecar has a JSONL protocol and currently runs in `dev_mock`
mode. Published packages should include a prebuilt sidecar at
`bin/<platform>/<arch>/agent-native-sidecar`; otherwise set
`AGENT_CORE_SIDECAR_BIN=/absolute/path/to/agent-native-sidecar`. If neither is
present, the bridge falls back to `cargo run` for development.
The sidecar process starts with a minimal environment: basic runtime/toolchain
variables plus the explicit production profile inputs below. Provider tokens
such as `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` are not forwarded to the Rust
sidecar.

Set `AGENT_CORE_NATIVE_PROFILE=prod` to start the dev sidecar in the production
profile boundary. Non-dev sidecar builds default to `prod`. That mode does not
fall back to dev mock state; it reports structured `unavailable` health until
these settings exist:

```text
AGENT_CORE_AUTH_TOKEN
```

The sidecar launcher forwards `https://api.puffo.ai` by default. Set
`AGENT_CORE_SERVER_URL` only when a local or staging backend should override the
production API. It also stores the production core database under
`AGENT_CORE_HOME` by default; set `AGENT_CORE_DATABASE_PATH` only to override
that file location.

When the auth token is present the sidecar reports `pairing_required`.
Production session requests then route through Rust `NativeCore::for_prod`,
which is wired to the `core` client SDK with HTTP server transport,
SQLite/SQLCipher persistence, macOS Keychain crypto behind the
`apple-keychain` feature, and Rust-side signed `x-puffo-*` HTTP auth headers.
As a provisional backend handoff path, `confirmPairing` can accept a
server-confirmed local pairing token; the running sidecar activates it
immediately and production builds with `apple-keychain` also persist it to
macOS Keychain. Route auth no longer uses that token as a bearer credential.
Production builds fail the handoff if Keychain persistence fails, and report
Keychain read errors explicitly instead of treating them as a missing token; dev
and non-Keychain builds keep it in sidecar memory only. The SQLCipher database
DEK is generated and loaded inside the Rust sidecar and stored as a local macOS
Keychain generic password; Node does not receive or forward that key material.
Backend PR #26 pairing merge/deploy, backend PR #25 space/invite replay
merge/deploy, native operator identity/session bootstrap verification, and the
final production packaging feature split are still the remaining integration
points; the default staged sidecar is still the dev build used by this MVP.
The concrete backend/Web pairing handoff is tracked in the source checkout at
`docs/AGENT_CORE_PAIRING_CONTRACT.md`.
Use `npm run build:native:prod` to build the non-dev production-profile sidecar
with the macOS Keychain provider enabled.
`npm run build:native` defaults to the dev sidecar unless
`AGENT_CORE_NATIVE_BUILD_PROFILE=prod` is set, so release packaging can choose
the staged native profile explicitly.
`npm publish` is guarded by `prepublishOnly` and refuses to publish unless
`AGENT_CORE_NATIVE_BUILD_PROFILE=prod` is set, preventing accidental release of
the dev sidecar profile. The same publish gate runs `npm run check:package`,
which verifies the generated npm tarball still contains the executable CLI and
native sidecar and does not include source, tests, scripts, package locks,
local env files, local DB files, or other generated development artifacts.
`publishConfig.registry` pins release publishes to `https://registry.npmjs.org/`
so a developer's local npm registry mirror cannot redirect a release.

There is also a process-per-command JSON CLI bridge for debugging:

```bash
cargo run --manifest-path crates/agent-native/Cargo.toml --features dev-tools --bin agent-native-cli -- health
cargo run --manifest-path crates/agent-native/Cargo.toml --features dev-tools --bin agent-native-cli -- dev-create-agent-identity --operator alice --agent alice-agent
cargo run --manifest-path crates/agent-native/Cargo.toml --features dev-tools --bin agent-native-cli -- dev-open-agent-session --slug alice-agent
cargo run --manifest-path crates/agent-native/Cargo.toml --features dev-tools --bin agent-native-cli -- dev-sync-once --handle dev:alice-agent
cargo run --manifest-path crates/agent-native/Cargo.toml --features dev-tools --bin agent-native-cli -- dev-process-pending --handle dev:alice-agent
```

Set `AGENT_CORE_NATIVE=cli` to force that fallback. Both native bridges create
operator-bound agent identities through the Rust client crate, including the
agent certificate's declared operator key and operator attestation path.
Production receive/send still needs product server pairing and transport; the
sidecar is the intended place to wire that before moving to N-API if needed.

## Runtime Loop

When an agent with a `coreIdentity` starts, `RuntimeManager` opens an opaque
core session handle, schedules `syncOnce -> processPendingMessages`, filters
self-messages by both local agent id and core agent slug, invokes the selected
provider session, and returns replies through the native core boundary.
For the localhost API, `start: true`, `/agents/:id/start`, and
`/agents/:id/restart` require a `coreIdentity`. Native creation uses
`operatorSlug` so the daemon can ask Rust core to create the local
operator-bound agent identity before starting message delivery. The MVP
Web-signed path can instead supply an already-signed/registered `coreIdentity`;
Node persists only high-level metadata, but production start still needs a Rust
core session capable of opening that identity. The source checkout records the
exact Web-signed MVP handoff at `docs/AGENT_CORE_WEB_SIGNED_MVP.md`. Local API
callers may only omit `coreIdentity.source` or send `web_signed`; `native` is
reserved for identities created by the Rust core path.
If the daemon restarts after an ungraceful exit, agents persisted as `running`
are resumed on startup only when they already have `coreIdentity`. Legacy
running agents without a core identity are marked `error` instead of starting a
provider-only process with no message loop.
Provider session state is persisted in each agent's `session.json`: Claude gets
a stable `--session-id`, and Codex captures the `codex exec --json` session id
and later calls `codex exec resume`.

For local dev smoke tests, start the daemon with `AGENT_CORE_DEV_ROUTES=1`.
That enables:

```text
POST /agents/:id/dev-inject
```

The body is `{ "senderSlug": "alice", "body": "@agent-slug status?" }`.
The route asks the sidecar to inject a dev encrypted channel message and then
ticks the agent once. It is disabled by default.

## Environment Detection

`GET /providers` and `agent doctor` report OS, Node, sandbox capability,
Claude/Codex readiness, and product server reachability. The default server URL
is `https://api.puffo.ai`; override it with:

```bash
AGENT_CORE_SERVER_URL=https://api.example.test
```

Public `/providers` responses omit local executable paths. Tokened requests and
`agent doctor` include those paths for diagnostics.

## Local API Auth

The daemon creates a local control token in `device.json` and prints it on
`agent start`. Discovery routes stay public:

```text
GET /health
GET /v1/info
GET /configuration?accountId=<current-account-id>&operatorSlug=<current-operator-slug>
GET /providers
```

`GET /v1/info` is a read-only compatibility shim for older Web builds that only
know the previous bridge discovery route. It reports basic daemon availability,
agent count, hostname, and public binding metadata; it does not implement the
old `/v1/pair` or `/v1/agents` secret-bundle bridge.

Web setup must use `/configuration` or `/health.binding` to compare the
daemon's public local binding metadata with the currently logged-in Web
account. A reachable daemon, native `core.status`, or Keychain/auth-token
readiness only proves that some local runtime state exists; it does not prove
the daemon is configured for the current account. Server-confirmed pairing
start/poll (`POST /pairing/start`, `GET /pairing/:pairingId`) are also public
loopback routes so Web can connect a fresh daemon before it has a local
management grant; direct `POST /pairing/confirm` remains token-protected.
Because a confirmed pairing poll may return a short-lived local grant, browser
pairing start/poll requests are accepted only from trusted Web origins by default
(`https://chat.puffo.ai`, `https://app.puffo.ai`, and local dev origins on
ports 3000/5173). Set `AGENT_CORE_ALLOWED_ORIGINS` to replace that browser
origin list for production-like runs.

The API returns permissive localhost CORS headers plus
`Access-Control-Allow-Private-Network: true` by default, so a browser-based Web
app can probe and call the daemon through modern Private Network Access
preflights during the MVP. Set `AGENT_CORE_ALLOWED_ORIGINS` to a
comma-separated list of exact HTTP(S) origins to restrict browser callers in
production-like runs:

```bash
AGENT_CORE_ALLOWED_ORIGINS=https://app.example.test,http://localhost:3000 agent start
```

With an allowlist configured, matching browser requests get their origin echoed,
non-matching browser origins receive `403`, and local CLI/curl calls without an
`Origin` header still use the normal token authorization path.
Responses also send `Cache-Control: no-store` and
`X-Content-Type-Options: nosniff` because diagnostics and logs can contain
local machine metadata.
POST routes with non-empty JSON bodies require `Content-Type: application/json`.

All other management routes fail closed unless a local authorization token is
present. The current user-facing token can be sent either way:

```text
Authorization: Bearer <token>
X-Agent-Core-Token: <token>
X-Agent-Core-Account-Id: <current-account-id>
X-Agent-Core-Operator-Slug: <current-operator-slug>
```

If both headers are present, any valid presented token authorizes the request.
Grant-management routes still require the local control token specifically.

The local control token can mint short-lived scoped local grants for the Web
management UI:

```text
GET /local-grants
POST /local-grants
DELETE /local-grants/:id
POST /local-control-token/rotate
```

Only the local control token is accepted on those grant-management routes.
Minted grants are stored hashed in `device.json`, default to 15 minutes, and
can call management routes through the same `Authorization` or
`X-Agent-Core-Token` headers. Account-bound grants also require matching
`X-Agent-Core-Account-Id` and `X-Agent-Core-Operator-Slug` headers, and
confirmed server pairing clears older local grants before minting the new
account-bound grant.
`GET /local-grants` returns grant metadata only; it never returns grant tokens
or stored token hashes.
`POST /local-control-token/rotate` returns a new local control token and clears
existing local grants, so it can disconnect previously authorized Web sessions.
The same recovery path is available from the terminal:

```bash
agent rotate-token
```

Running `agent start` again while the daemon is already alive is idempotent: it
prints the existing daemon URL and local control token instead of failing.
Use `agent start --json` when a bootstrapper or Web helper needs a single
machine-readable line with `status`, `url`, `token`, and version metadata.
The CLI validates `--port` and `AGENT_CORE_PORT` before binding and treats a
daemon pid file as live only when its localhost `/health` response matches the
same daemon `instanceId` or, for legacy pid files, the same state directory.
Public `/health` calls do not reveal the local state path; the CLI includes the
local control token when it needs that private match.

## Runtime Status

Use:

```text
POST /agents/preview
POST /agents
GET /agents
GET /agents/:id
DELETE /agents/:id
GET /agents/:id/status
GET /agents/:id/logs?maxLines=200
GET /agents/:id/policy
POST /agents/:id/policy?preview=true
POST /agents/:id/policy
POST /agents/:id/recheck
```

to inspect persisted agent config and live runtime attachment, provider status,
core session, message loop, poller, and in-progress tick state.
`POST /agents/preview` validates a draft agent and returns the effective policy
without persisting an agent, creating a core identity, starting a provider, or
projecting credentials.
`DELETE /agents/:id` stops the runtime and removes local agent state; it does
not delete an external `projectPath`.
`POST /agents/:id/policy` updates `accessMode`, `projectPath`,
`networkAccess`, `deniedTools`, and `fileAccess`; running agents are restarted
so the new environment policy takes effect.
`GET /agents/:id/policy` returns a side-effect free effective policy preview
without provider environment variables or credential projection.
`POST /agents/:id/policy?preview=true` previews a proposed policy update
without persisting it or restarting the agent.
`POST /agents/:id/recheck` re-runs readiness detection for the selected
provider and returns the current provider check for that agent.

## macOS Sandbox

On macOS, `safe` and `project` agents are launched through `sandbox-exec` with
a generated profile by default. `safe` allows writes under the agent
home/workspace; `project` also allows the selected project path. `trusted`
intentionally does not apply this sandbox.
Provider processes never receive daemon-internal `AGENT_CORE_*` environment
variables inherited by the daemon. `safe` and `project` get a minimal
environment plus isolated `HOME`; `trusted` inherits the user's ordinary
environment after stripping those daemon-internal variables.

For local debugging of unrestricted `safe`/`project` agents, set:

```bash
AGENT_CORE_SANDBOX=off
```

Restrictive policy still forces sandboxing even when that debug opt-out is set.
Set `networkAccess: "deny"` on an agent to omit network access from that
agent's generated sandbox profile. `AGENT_CORE_NETWORK=off` makes newly created
agents default to `networkAccess: "deny"`. Agents can also provide
`deniedTools`, a list of additional executable names or absolute paths to block
inside the generated sandbox. The profile also denies execution of sensitive
local tools such as
`security`, `lsof`, `ps`, `dtrace`, `fs_usage`, `scutil`, `launchctl`, and
`osascript`; append additional absolute executable paths with
`AGENT_CORE_DENIED_EXECUTABLES`. The doctor report includes whether
`sandbox-exec` is available.
Agents can also provide `fileAccess.readablePaths` and
`fileAccess.writablePaths` to grant extra absolute existing directories to a
`safe` or `project` sandbox. Writable paths are readable too. Send
`fileAccess: null` in a policy update to clear those extra file resources.
Use `providerConfigPaths` for the separate case where a provider expects config,
MCP, slash-command, or skill files inside its `HOME`, for example
`.claude/commands` or `.codex/skills`. Those entries are relative to the user's
real home, constrained to supported provider-owned config paths, copied into
the isolated agent home, and refused if a source path or nested entry is a
symlink. Broad roots such as `.claude` or `.codex` are intentionally refused.
Send `providerConfigPaths: null` to clear them.
When a generated sandbox is required but `sandbox-exec` is unavailable,
`agent start`/runtime start fails with `sandbox_unavailable` instead of waiting
for the first provider message.

For `safe` and `project`, provider CLIs run with an isolated `HOME`. To keep
already-logged-in users working without exposing the whole real home directory,
the policy layer projects only a small provider credential allowlist into the
agent home:

```text
codex:  .codex/auth.json, .codex/config.toml
claude: .claude.json, .claude/.credentials.json, .claude/settings.json
```

Set `AGENT_CORE_CREDENTIALS=off` to disable this projection.
Credential projection only copies regular allowlisted files, refuses symlinked
source credentials and source credential directories, rejects symlinked
isolated home/workspace roots, and will not write through symlinked target
directories. Explicit `providerConfigPaths` use the same source/target symlink
refusal rules and can copy regular files or directories recursively. Projection
is bounded by file count, per-file size, total bytes, and recursion depth; files
over the limits are skipped instead of blocking daemon startup.
Local state and log files use the same safety rule: state roots, agent
directories, log directories, and JSON/log files are rejected if they are
symlinks, and writes stay under the daemon state root.

## Production Unblock

The local MVP is implemented and verified, but production message delivery
still depends on upstream `core` and backend/product contracts. When browsing
the source repository, use `docs/AGENT_CORE_PRODUCTION_UNBLOCK_CHECKLIST.md`
as the short owner-by-owner checklist. The detailed backend draft remains in
`docs/AGENT_CORE_SERVER_CONTRACT_NEEDED.md`, the focused pairing handoff is in
`docs/AGENT_CORE_PAIRING_CONTRACT.md`, and the Rust submodule patch handoff is
in `docs/AGENT_CORE_CORE_UPSTREAM_HANDOFF.md`.
Those `docs/` files are intentionally repository-only and are not shipped inside
the npm runtime package.

## Commands

```bash
npm install
npm run build
npm run build:native
npm run build:native:prod
npm test
AGENT_CORE_NATIVE_BUILD_PROFILE=prod npm publish
node dist/src/cli/index.js doctor
node dist/src/cli/index.js start
node dist/src/cli/index.js rotate-token
```

The default local API port is `63387`.
