# Agent Core Implementation Audit

Objective: implement the redesigned CLI-local agent core as a Node/TypeScript
local daemon with a Rust native boundary over the `core` submodule.

This audit maps the design requirements to concrete artifacts in the current
`agent-core/` package and calls out what is still blocked by backend/product
contracts.

## Objective Decomposition

The active implementation request is interpreted as these concrete deliverables:

1. extract/rewrite the `cli-local` agent runtime into a neutral Node/TypeScript
   package with a one-command local daemon CLI;
2. mount the Rust `core` repository under `agent-core/core` and keep
   cryptographic identity/message work behind a Rust-native boundary;
3. expose a localhost API for Web discovery, provider readiness, agent
   lifecycle, policy, status, logs, and diagnostics;
4. support Claude Code and Codex CLI sessions with persisted provider session
   state;
5. implement the macOS MVP policy layer: filesystem mapping/filtering,
   credential projection, network deny, denied tools, and generated sandbox
   launch;
6. implement the message loop rule where the agent decides whether to reply,
   while explicit mention/DM requires a visible result;
7. provide docs and verification commands that prove the local/dev package path
   works and clearly identify what is still not production-ready.

## Prompt-To-Artifact Checklist

| Prompt requirement / named deliverable | Artifact evidence inspected | Verification status |
| --- | --- | --- |
| "不要 `puffo-*` 前缀" for the new runtime | `agent-core/package.json` publishes under scoped npm name `@puffo-ai/agent-core`, installs bin `agent`; Rust crate is `agent-native`; README documents that runtime/binary names remain neutral and the org scope is only for npm publishing | Covered |
| Put `core` as a submodule inside `agent-core` | `.gitmodules` maps `agent-core/core` to `https://github.com/puffo-ai/core.git`; parent gitlink records PR #18 commit `ece389a...`; upstream PR https://github.com/puffo-ai/core/pull/18 contains the required Rust client API changes, Rust-side agent identity publication, signed HTTP helper, and current backend message/space/invite read/write route-shape alignment | Covered for fresh clones while the PR branch commit remains reachable; PR #18 still must merge and the parent submodule pointer should then move to the merged upstream revision before release |
| "用 `/agent/core` 的加密解密 key API" / Node must not handle keys | `agent-core/crates/agent-native/src/lib.rs` wraps `ClientSdk` use cases; production SQLCipher DEK generation/loading is inside Rust and stored in macOS Keychain; production auth token activation can happen through native `confirmPairing` and production `apple-keychain` builds persist it in Keychain; `src/native/core.ts` and `src/native/sidecar-core.ts` expose opaque handles and opened message fields to Node; sidecar spawn uses a minimal env that does not forward provider API keys; MVP Web-signed identity attachment lets Web pass `coreIdentity` metadata without giving Node private keys | Covered for local/dev loop, production-profile DB/auth handling, and Web-signed metadata attachment; Web-signed association keeps browser-key security tradeoffs and final backend/native pairing remains blocked |
| One-line install/start style local daemon | `agent-core/package.json` bin `agent`; `src/cli/start.ts`; `src/daemon/*`; `scripts/bootstrap-macos.sh` for Web-hosted macOS bootstrap; smoke test installs package into a temp npm prefix, starts daemon, checks `/health`, uses the printed control token, exercises local grant issuance/revocation, and stops it | Covered on current macOS arm64 machine |
| Browser can detect daemon and providers | `GET /health`, legacy read-only `GET /v1/info`, and `GET /providers` in `src/api/server.ts`; local API docs describe public discovery, redacted provider paths, and actionable crashed-provider fix commands | Covered |
| Management API with auth and local grants | `src/api/auth.ts`, `src/api/server.ts`, `src/state/store.ts`, `src/cli/rotate-token.ts`; tests cover token enforcement, fail-closed auth, control-token-only local grant listing/issuance/revocation, local control-token rotation through API and CLI, hashed grant expiry/revocation/pruning, grant metadata listing without returning token material, public loopback server-confirmed pairing start/poll before Web has a grant, protected direct pairing confirm, and daemon-minted local Web grants on the one-time server-confirmed pairing token handoff | Covered for local control-token grant issuance/rotation and PR #26-style local grant handoff; product still owns whether Web should request/allow that grant |
| Create/start/stop/restart/reset/recheck/delete agents | `src/runtime/runtime-manager.ts`, `src/api/server.ts`, `src/state/store.ts`; lifecycle tests cover these paths, restart-on-policy-update, localhost API rejection of `start`/`restart` when the agent has no `coreIdentity`, Web-signed `coreIdentity` attachment without native identity creation, and policy-update rejection when it would implicitly restart a running no-identity agent | Covered |
| Claude/Codex provider bridge and sessions | `src/providers/claude/*`, `src/providers/codex/*`; tests cover Claude `--session-id`, Codex `exec --json`, Codex resume/session parsing, missing executable classification | Covered |
| Agent decides reply; `@`/DM must respond | `src/messaging/*`; runtime tests cover fallback on `mustRespond` silent output and slug-boundary mention handling; delivery tests cover empty must-respond replies and redacted visible provider errors | Covered in dev message loop |
| macOS sandbox, file mapping/filtering, network control, denied tools | `src/policy/*`, `src/providers/process/child-process.ts`; tests cover safe/project sandbox default, trusted opt-out, global and per-agent network deny, explicit extra `fileAccess` readable/writable directories, explicit allowlisted provider-owned `providerConfigPaths` projection for provider-home config/skills, credential projection, credential-projection opt-out, symlink refusal, denied executable policy | Covered for macOS MVP only |
| Logs and diagnostics | `src/logs/*`, `/diagnostics`, `/agents/:id/logs`, `/agents/:id/status`; tests cover redaction, bounded tail, Web-exposed `maxLines` query validation, private file modes, symlink rejection, and redacted live provider status errors | Covered |
| Production backend contract handoff | `docs/AGENT_CORE_SERVER_CONTRACT_NEEDED.md` records the 2026-05-06 backend snapshot and the signed `x-puffo-*` auth mismatch; backend PR https://github.com/puffo-ai/puffo-server/pull/25 targets `dev` and adds signer-id replay persistence plus invite proof fields; backend PR https://github.com/puffo-ai/puffo-server/pull/26 targets `dev` and implements server-side local daemon pairing; `src/pairing/server-pairing.ts` now consumes the PR #26 start/poll routes and hands the one-time `authToken` to native confirm without echoing it | Partially unblocked; local daemon pairing gateway is implemented and tested against a mock PR #26 server, while backend PR #25/#26 merge/deploy from `dev` and native/product bootstrap verification remain unresolved |
| Package gate | `npm test`, `npm run check:package`, `npm run smoke:package`, `npm pack --dry-run`, `npm pack --json`, Rust cargo tests | Covered on current machine; pack contains macOS arm64 sidecar only; scoped npm publish config is pinned to `registry=https://registry.npmjs.org/` and `access=public` |
| Production device pairing | `/pairing/start`, `GET /pairing/:id`, and `/pairing/confirm` exist locally; `ServerPairingGateway` calls backend PR #26 start/poll routes; start/poll are public loopback routes so Web can pair before it has a local grant, but browser callers must use a trusted Origin before a confirmed poll can return a local grant; direct confirm remains token-protected; confirmed server poll forwards the one-time auth token into native `confirmPairing` and can mint a short-lived local Web grant when the server asks for `daemon_mints` | Local gateway implemented and covered by API tests; blocked until PR #26 merges/deploys and product/native bootstrap semantics are verified end to end |
| Production agent identity registration | local Rust can create operator-bound agent identity; pulled `puffo-server/main` now has `POST /agents` plus pending-agent `POST /certs/slug_binding` persistence; `ClientSession::create_and_publish_agent_identity` and production sidecar `createAgentIdentity` publish the identity/device/attestation bundle from Rust and re-sign the slug binding when the backend allocates the canonical agent slug; Web-signed MVP can attach already-registered `coreIdentity` metadata without native identity creation | Native path implemented locally; Web-signed metadata path covered for MVP; production transport still needs native session/key bootstrap verification |
| Production encrypted message receive/send | dev sidecar can open/send local encrypted messages; pulled `puffo-server/main` now has `POST /messages`, `GET /messages/pending`, `POST /messages/ack`, space read/write routes, and status/processing routes; backend PR #25 adds signer-id replay plus invite proof material; backend PR #26 adds server-confirmed daemon pairing consumed by `ServerPairingGateway`; `agent-native` has feature-gated `NativeCore::for_prod` construction plus signed `x-puffo-*` header injection from Rust | Partially covered; still blocked by end-to-end pairing/bootstrap verification, backend PR #25/#26 merge/deploy from `dev`, and production release packaging choice |

## Checklist

| Requirement | Evidence | Status |
| --- | --- | --- |
| Use neutral package/binary names, no new `puffo-*` runtime prefix | `agent-core/package.json` package name is `@puffo-ai/agent-core`; bin is `agent`; Rust crate is `agent-native`; the product org scope is used only to avoid the occupied unscoped npm name | Done |
| Mount Rust product core under `agent-core` as a submodule | `.gitmodules` has `path = agent-core/core`; `agent-core/crates/agent-native/Cargo.toml` depends on `../../core/crates/client` and `../../core/crates/types`; parent gitlink records PR #18 commit `ece389a...`; `tests/package-metadata.test.ts` verifies the recorded gitlink matches the checked-out core revision | Covered for the current review branch; after PR #18 merges, the pointer should move to the merged upstream revision |
| One-command local daemon startup | `src/cli/start.ts`, `src/daemon/daemon.ts`, package bin `agent`; default API `127.0.0.1:63387`; repeat `agent start` is idempotent and reprints the connection info; `agent start --json` returns the same connection contract as one machine-readable line; invalid ports are rejected before listen; tests spawn the built CLI, assert the stable Web-parseable three-line connection block and JSON connection output, read `/health`, and stop the daemon | Done |
| Local daemon single-instance lifecycle | `src/daemon/lockfile.ts`, `src/daemon/lifecycle.ts`, `src/daemon/supervisor.ts`; daemon tests cover resume, failed resume, legacy running-agent resume refusal when `coreIdentity` is missing, best-effort stop-all cleanup, port conflicts, corrupt daemon pid file cleanup, stale pid files whose pid was reused, and instance-id health verification when the local control-token file is corrupt | Done |
| Public discovery routes | `GET /health`, legacy read-only `GET /v1/info`, `GET /providers` in `src/api/server.ts`; CORS includes Private Network Access preflight support for browser localhost calls plus `no-store`/`nosniff` response hardening; default CORS remains permissive for MVP discovery, while `AGENT_CORE_ALLOWED_ORIGINS` can restrict browser callers to exact HTTP(S) origins; Host validation only accepts exact loopback hosts with valid optional ports and runs before preflight handling; public health does not expose `stateHome`, `/v1/info` exposes only daemon availability/count/binding metadata, and public providers do not expose local executable paths unless the local token is presented | Done |
| Management routes protected by local authorization | `src/api/auth.ts`, `src/api/server.ts`, `src/cli/rotate-token.ts`; tests cover control-token enforcement, control-token-only short-lived local grant listing/issuance/revocation, control-token-only rotation that invalidates old token/grants, CLI rotation against stopped and running daemons, hashed local access grants, fail-closed behavior when no token is configured, and PNA preflight headers | Done |
| Provider/environment detection | `src/doctor/provider-detector.ts`, Claude/Codex/sandbox detectors, server connectivity object; `agent doctor` emits structured redacted reports; provider executable `--version` probe failures are reported as `ready: false`, `reason: "crashed"`, with repair `fixCommand`; sandbox detection uses the same executable lookup as runtime launch; `POST /agents/:id/recheck` re-runs readiness detection for one agent's selected provider | Done |
| Local state persistence | `src/state/store.ts`, `src/state/paths.ts`, `src/platform/safe-directory.ts`, `src/platform/agent-id.ts`; tests cover absolute state-root normalization including `AGENT_CORE_HOME=~/...`, agent config, legacy normalization, control token reuse, corrupt control-token file regeneration, hashed local access grant creation/expiry/revocation/pruning, permission tightening, state root/file/agent-dir symlink rejection for reads, writes, and deletes, unsafe/oversized agent id rejection at the state boundary, invalid or corrupt legacy agent directory skipping during list, and collision-resistant concurrent writes | Done |
| Create/start/stop/restart/reset/delete agent | `src/runtime/runtime-manager.ts`, `src/state/store.ts`, API routes; tests cover lifecycle, Claude session reset, provider session id persistence after message handling, best-effort stop/delete when provider stop fails, deleted-agent tick exit, local agent state deletion without touching `projectPath`, and API-level prevention of provider-only starts for agents without core identity | Done |
| Claude/Codex provider bridge | `src/providers/claude/*`, `src/providers/codex/*`; provider command tests cover prompt metadata, Claude `--session-id`, Codex `exec resume`, Codex JSON output/session parsing, resolved executable paths, session ids retained on provider errors, missing executable classification, start-time sandbox availability validation, and `sandbox-exec` unavailable classification | Done |
| Agent decides when to reply; `@`/DM must respond | `src/messaging/message-loop.ts`, `src/messaging/delivery.ts`; Rust sidecar now uses slug-boundary mention matching; tests cover must-respond fallback, empty must-respond replies, redacted visible errors, and `@slug-extra` non-match | Done |
| Native Rust boundary hides crypto/key material from Node | `src/native/core.ts`, `src/native/sidecar-core.ts`, `crates/agent-native/src/lib.rs`; Node receives opaque handles and opened-message fields only | Done for local/dev loop |
| Rust sidecar persistent process | `crates/agent-native/src/bin/agent-native-sidecar.rs`, `src/native/sidecar-core.ts`; tests cover persistent state, request timeout, minimal sidecar environment without provider API key inheritance, default production server URL/database path forwarding, native `confirmPairing` production auth token activation, missing packaged binary diagnostics, absolute-path enforcement for native binary overrides, missing override-path diagnostics before spawn, explicit production-profile missing-config health, complete production-profile `pairing_required` health without dev mock fallback, production session requests routing through native provider construction, and production JSONL serving without `dev-tools` or an explicit profile env var | Done |
| Package staged native sidecar | `scripts/build-native.mjs`, `scripts/make-bin-executable.mjs`, `scripts/check-pack-manifest.mjs`, package `prepack`, `prepublishOnly`, and `scripts/assert-release-profile.mjs`; `npm test` now rebuilds native sidecar before integration tests; `npm pack --json` remains machine-readable and tarball bin/native files are executable; `npm run build:native:prod` and `AGENT_CORE_NATIVE_BUILD_PROFILE=prod npm pack` build/stage the non-dev production-profile sidecar; `npm publish` refuses to proceed unless `AGENT_CORE_NATIVE_BUILD_PROFILE=prod` is set and also runs the pack-manifest verifier before publish | Done for macOS arm64 dev machine; publish is guarded against accidental dev-sidecar release and unexpected tarball contents |
| Access modes and macOS sandbox policy | `src/policy/*`, `src/providers/process/child-process.ts`; tests cover sandbox wrapping, default macOS sandboxing for `safe`/`project`, debug opt-out only for unrestricted policy, automatic sandboxing for restrictive policies, global and per-agent network deny, denied tools, explicit `fileAccess.readablePaths`/`writablePaths` canonicalization and sandbox root mapping, explicit allowlisted provider-owned `providerConfigPaths` file/directory projection with source/target symlink refusal, projection budget limits, and broad-root/cache/history rejection, safe/project minimal provider env without daemon-internal `AGENT_CORE_*`, trusted env with daemon-internal `AGENT_CORE_*` stripping, credential projection and global credential-projection opt-out, broad `PATH` root filtering for sandbox readable roots, source/isolated root/target symlink hardening including side-effect free previews, API and persisted project/file-access/provider-config path canonicalization, and invalid project/file-access/provider-config path rejection | Done for macOS MVP |
| Logs and diagnostics | `src/logs/*`, `/diagnostics`, `/agents/:id/logs`, `/agents/:id/status`; tests cover write-time and read-time secret redaction including JSON `authToken`/API-key fields, private file modes, bounded log tail reads, Web-exposed `maxLines` query validation, log dir/file symlink rejection for reads and writes, unsafe/oversized agent id rejection at the log boundary, redacted API internal errors, redacted runtime/provider errors, and redacted live provider status errors | Done |
| Web-facing local API contract | `docs/AGENT_CORE_LOCAL_API.md` documents localhost discovery, legacy read-only `/v1/info` discovery compatibility, auth, local grants, agents, Web-signed `coreIdentity` attachment, server-confirmed pairing gateway routes, policy, `fileAccess`, `providerConfigPaths`, diagnostics, dev injection, configurable CORS/PNA, and error shapes; API tests cover create/policy validation including boolean `start`, string `instructions`, `fileAccess` path validation/canonicalization, `providerConfigPaths` validation and preview non-persistence, trusted-mode rejection for ignored restrictive policy, side-effect free create/current/proposed effective policy preview without env exposure or persistence, stale project preview errors as `400 bad_request`, no-identity start/restart/policy-restart rejection, Web-signed `declaredOperatorPublicKey` requirement, default wildcard CORS for discovery, configured origin allowlist blocking, wildcard allowlist fallback, invalid allowlist-origin rejection, public PR #26-style server pairing start/poll before local grant issuance, trusted Origin enforcement before Web can receive a pairing-created local grant, and protected direct pairing confirm without token echo | Done |
| Production device pairing | API routes exist locally; `ServerPairingGateway` calls backend PR #26 start/poll routes; pairing start/poll are public loopback routes with trusted browser Origin enforcement while direct confirm is token-protected; confirmed poll passes the one-time auth token into native confirm without echoing it | Blocked until PR #26 merges/deploys and product/native bootstrap semantics are verified end to end |
| Production agent identity registration | Rust can create local operator-bound agent identity; generated default agent slugs are bounded before calling the native core boundary; pulled backend can register and bind agent cert material; native production `createAgentIdentity` now publishes through Rust and keeps Node on high-level status fields only; API can also attach Web-signed `coreIdentity` metadata for the MVP | Partial: native route is wired locally but real success is still blocked until pairing restores/provisions the operator identity in the production store; Web-signed path is an MVP shortcut and still needs native session/key bootstrap for production transport |
| Production encrypted message transport | Dev sidecar opens/sends messages through `core`; `core` has HTTP provider route mapping plus `BlockingHttpTransport`; pulled backend has message send/pending/ack and space read/write routes; backend PR #25 adds signer-id replay plus invite proof material; backend PR #26 adds server-confirmed daemon pairing; `agent-native` has feature-gated `NativeCore::for_prod` provider construction with macOS Keychain crypto, SQLite/SQLCipher persistence, Rust-side Keychain-backed SQLCipher DEK, default `https://api.puffo.ai` server URL/default local database path forwarding with env overrides, Rust-side production auth token Keychain fallback, and Rust-side signed `x-puffo-*` HTTP header injection using the active native session signer | Partial / Blocked by end-to-end pairing/bootstrap verification, backend PR #25/#26 merge/deploy from `dev`, and production release packaging choice |
| Web-to-local authorization after install | Local token exists; daemon can mint, persist, revoke, and verify hashed short-lived scoped local grants; grants can call management routes but cannot mint or revoke other grants | Done for local control-token bootstrapping |
| Web-to-local authorization after pairing | Local grant APIs exist; `ServerPairingGateway` mints a short-lived local Web grant when a confirmed PR #26 poll returns `localWebGrant.mode = "daemon_mints"`, only on the one-time auth-token handoff | Implemented for the current PR #26 hint; product still decides whether Web should request/allow this grant in the UX |
| Web-signed MVP handoff code | `puffo-core-han-group/client/web/src/agent-core/client.ts`, `puffo-core-han-group/client/web/src/agent-core/provision.ts`, `puffo-core-han-group/client/web/src/agent-core/types.ts`, and the `src/agent-core/index.ts` barrel add a non-UI Web integration surface for the new localhost API. The helper keeps Web-side certificate signing/server registration and hands localhost only public `coreIdentity` metadata with bearer/local-grant auth, not old `x-puffo-*` bridge auth or secret bundles. It installs a server-confirmed pairing `localGrant` for following management calls only when the pairing status is `confirmed`, or exchanges a printed control token for a scoped local grant and switches to that. `client/web/tests/setup.ts` now polyfills only broken Node test `localStorage`/`sessionStorage` objects, and `SignupPage` clears its invite debounce timer on unmount so full Web tests do not fail after teardown | Web PR https://github.com/puffo-ai/puffo-core-han-group/pull/52 is open and mergeable from `feature/agent-core-web-signed-mvp` to `main`; GitHub CI `Check & Test` and Web `Type-check + build` are green; local focused Web tests pass, and the earlier full Web unit tests/typecheck passed after generating the ignored wasm package; UI store/page migration is still a separate Web task |

## Core Upstream Reproducibility

Current `agent-core` records `agent-core/core` at the review branch commit
`ece389a...`, so a fresh clone can reproduce the verified working tree as long
as that PR branch commit remains reachable from `puffo-ai/core`. The upstream
patch base is still recorded separately as
`docs/patches/agent-core-core-upstream.base` (`c623bd6...`) so reviewers can
verify the PR diff independently of the parent gitlink. The PR diff adds the
operator-bound agent identity API used by `crates/agent-native`, Rust-side
agent identity publication, dev-only client test gating, and the real blocking
HTTP transport. The changed upstream files include:

```text
Cargo.lock
crates/client/Cargo.toml
crates/client/src/providers/mod.rs
crates/client/src/actions/spaces/create_channel.rs
crates/client/src/api/command.rs
crates/client/src/api/session.rs
crates/client/src/domain/identity/model.rs
crates/client/src/ports/crypto.rs
crates/client/src/ports/mod.rs
crates/client/src/ports/server/account.rs
crates/client/src/providers/crypto/core_crypto.rs
crates/client/src/providers/crypto/unavailable.rs
crates/client/src/providers/server/http/mod.rs
crates/client/src/providers/server/http/provider.rs
crates/client/src/providers/server/http/transport.rs
crates/client/tests/contract_client_api.rs
crates/client/tests/contract_profiles.rs
crates/client/tests/contract_server_ports.rs
crates/client/tests/contract_store_ports.rs
crates/client/tests/scenario_blobs.rs
crates/client/tests/scenario_cloud_backup.rs
crates/client/tests/scenario_device_revocation.rs
crates/client/tests/scenario_identity.rs
crates/client/tests/scenario_messages.rs
crates/client/tests/scenario_outbox.rs
crates/client/tests/scenario_password_auth.rs
crates/client/tests/scenario_smoke.rs
crates/client/tests/scenario_spaces.rs
crates/client/tests/scenario_sync.rs
specs/001-client-sdk/contracts/client-api.md
```

The parent `agent-core` sidecar also has local changes for
`AGENT_CORE_NATIVE_PROFILE=prod`, feature-gated production provider wiring, and
the matching Node integration tests.

Before this work is mergeable or publishable, PR #18
(https://github.com/puffo-ai/core/pull/18) needs to land in the
`puffo-ai/core` repository, and the parent `agent-core/core` submodule pointer
should be updated from the PR branch commit to the merged upstream revision.
The upstream patch groups, affected files, and verification commands are
tracked in [`AGENT_CORE_CORE_UPSTREAM_HANDOFF.md`](AGENT_CORE_CORE_UPSTREAM_HANDOFF.md).
The current PR diff from the recorded upstream patch base is also exported to
[`patches/agent-core-core-upstream.patch`](patches/agent-core-core-upstream.patch)
so the required upstream patch can be reviewed and applied without relying on
this working tree or the GitHub PR branch.

## Latest Verification Commands

Run from `agent-core/`:

```bash
npm test
node --test dist/tests/api-server.test.js
bash -n scripts/bootstrap-macos.sh
npm run check:package
npm run check:core-patch
npm run export:core-patch
npm run smoke:package
node scripts/assert-release-profile.mjs
AGENT_CORE_NATIVE_BUILD_PROFILE=prod node scripts/assert-release-profile.mjs
npm publish --dry-run
AGENT_CORE_NATIVE_BUILD_PROFILE=prod npm publish --dry-run
npm run build:native:dev
npm run build:native:prod
AGENT_CORE_NATIVE_BUILD_PROFILE=prod npm pack --dry-run
AGENT_CORE_NATIVE_BUILD_PROFILE=prod npm run smoke:package
npm pack --dry-run
npm pack --json
cargo test --manifest-path crates/agent-native/Cargo.toml
cargo test --manifest-path crates/agent-native/Cargo.toml --features dev-tools
cargo check --manifest-path crates/agent-native/Cargo.toml --features apple-keychain --bin agent-native-sidecar
cargo check --manifest-path crates/agent-native/Cargo.toml --features dev-tools,apple-keychain --bin agent-native-sidecar
AGENT_CORE_SERVER_URL=http://127.0.0.1:9 node dist/src/cli/index.js doctor
(cd core && cargo build -p puffo-client)
(cd core && cargo test -p puffo-client)
(cd core && cargo test -p puffo-client --features dev-tools)
(cd core && git apply --check --reverse ../../docs/patches/agent-core-core-upstream.patch)
```

The current local/dev implementation passes these checks. The latest focused
API/state/sidecar/CLI test run reports 104 passing tests, including legacy
read-only `/v1/info` daemon discovery compatibility, trusted browser Origin
enforcement for public pairing start/poll before a confirmed poll can return a
local grant,
explicit `404 not_found` coverage for legacy `/v1/pair` and secret-bundle
`/v1/agents` routes,
configurable browser CORS origin allowlisting and allowlist validation, local
control-token rotation, pairing confirm input validation, pairing start
object-body validation, pairing auth-token handoff without response echo, and
multi-header local auth
where `Authorization` and `X-Agent-Core-Token` are treated as alternatives
without allowing scoped grants to mint or revoke grants. It also verifies that
legacy Web bridge `x-puffo-*` signed request headers do not authenticate new
management routes. It also covers
`start: true` requiring `operatorSlug` or `coreIdentity`,
`agentSlug` requiring `operatorSlug` unless `coreIdentity` is supplied,
Web-signed `coreIdentity` attachment bypassing native identity creation
readiness, rejection of unsupported `coreIdentity` fields so Web cannot pass a
secret bundle into daemon state, explicit rejection of the legacy top-level
`identity_bundle` secret-bundle handoff from the old Web bridge, rejection of
API-supplied `coreIdentity.source = "native"` so only Rust-created identities
can claim native source, a shared 4096-character bound for
`declaredOperatorPublicKey` across API/runtime/state/native bridge boundaries,
actual identity creation failing closed when
native core identity creation is unavailable, and `/start`/`/restart` refusing
agents without `coreIdentity`.
It also covers actual identity creation failing before persistence when native
core exists but is not ready for local agent identity creation.
It also covers actual policy updates refusing running no-identity agents when
the update would implicitly restart the provider process.
It also covers
bounded, redacted agent log reads through the Web-exposed `maxLines` query and
side-effect free create/current/proposed effective policy previews without provider env exposure,
including `fileAccess` validation/canonicalization and non-persistence of
previewed file-resource changes, plus `providerConfigPaths` validation and
non-persistence of previewed provider-home projection changes,
including stale project paths reported as `400 bad_request`. The latest
full `npm test`
run reports 206 passing tests,
including the local API, real CLI start/stop, runtime loop, native sidecar,
sandbox policy, state/log symlink hardening, provider bridge coverage,
publish-valid package metadata, the release-profile prepublish gate, package
file allowlist, root `.gitignore` boundary coverage, the macOS bootstrap script
syntax check, and the stable
three-line `agent start` connection output contract consumed by Web/bootstrap
flows plus the `agent start --json` machine-readable output contract. It also covers `agent rotate-token` against stopped and running daemon
states, verifies that daemon-created native sidecars derive their default
production database path from the daemon's actual state root, and covers native
`confirmPairing` activation of a production auth token plus the localhost API
handoff path that forwards the token to native core without returning it in the
response. The policy suite also covers provider config projection budget
limits, and the copy path opens source files with no-follow semantics,
re-checks the opened file identity, and reads through the same handle within
the configured budget before writing into the isolated agent home.
The API suite now also covers `ServerPairingGateway` against a mock PR #26
server: local `POST /pairing/start` sends daemon metadata to
`POST /agent-core/pairings`, local `GET /pairing/:id` polls the backend,
forwards the one-time `authToken` to native `confirmPairing`, does not echo that
token, derives `localApiOrigin` from the actual loopback request host instead of
trusting the Web body, mints a short-lived local Web grant only on the one-time
token poll, and refuses unsafe server-requested grant TTLs outside the 1 second
to 15 minute pairing-specific window. It also validates backend pairing
response shapes at runtime, including start-response `pairingId`, `confirmUrl`,
`expiresAt`, and bounded `pollAfterMs`, so unknown statuses or malformed fields
fail closed instead of being forwarded to Web as trusted state.
The latest package manifest check passed with 249 files, the default package
smoke started the published `agent` binary and verified `/health`, root
`git diff --check` passed, and no generated `.tgz` package artifacts remained
under `agent-core/`.
Passing them does not prove production readiness because the blocked items
above require backend contracts and local sidecar production-profile wiring
beyond the feature-gated provider construction and non-dev sidecar build now
present.
The latest `npm run test:native` run also passes the Rust native crate tests
with and without `dev-tools`, plus `cargo check` for the macOS
`apple-keychain` sidecar profile.
The release-profile assertion intentionally fails without
`AGENT_CORE_NATIVE_BUILD_PROFILE=prod` and passes with it, so local
development packs remain convenient while `npm publish` is guarded.
The latest `npm publish --dry-run` check fails before packing without the prod
profile, and succeeds with `AGENT_CORE_NATIVE_BUILD_PROFILE=prod`. That dry-run
also verified the package `bin.agent` path is publish-valid after changing it
to `dist/src/cli/index.js`.

`bash -n scripts/bootstrap-macos.sh` passes for the Web-hosted macOS bootstrap
script. The package metadata tests also cover the bootstrap path that installs
into the user-local npm prefix `$HOME/.agent-core/npm` and starts the installed
`agent` binary directly, plus the no-existing-Node path that downloads a
user-local Node.js tarball, verifies its checksum, exposes its bundled `npm`,
and then installs/starts `agent`. The Web bootstrap does not depend on system
npm write permissions, Homebrew, or the global npm bin directory being on
`PATH`.
The README now makes the Web-hosting responsibility explicit: the bootstrap
script is intentionally not shipped inside the npm package. Package metadata
tests assert that `scripts/` stays out of the published `files` allowlist.
The bootstrap also refuses unsafe `AGENT_CORE_INSTALL_PREFIX` and
`AGENT_CORE_NODE_DIR` overrides such as root, `$HOME`, `$HOME/.agent-core`, and
common system directories before creating directories or replacing the
user-local Node install. It also refuses symlinked path components and `..`
traversal for the install prefix, the user-local Node directory, and the
temporary download directory; package metadata tests cover the unsafe override
and symlink rejection paths. The bootstrap also validates `AGENT_CORE_NODE_MAJOR`
before any Node.js download, rejecting non-integer values and versions below
the required Node.js major, and it rejects non-HTTPS `AGENT_CORE_NODE_DIST_BASE`
download overrides, including overrides containing whitespace or newlines.
Node.js checksum and tarball downloads also pass `curl` protocol restrictions
so redirects cannot downgrade away from HTTPS. The bootstrap runs with `umask
077`, and package metadata tests verify that the npm install subprocess
inherits that private umask. It also re-checks install/download paths after
creating parent directories and before replacing the fallback Node.js
directory. The bootstrap starts only the expected `agent` binary from the
selected install prefix and refuses to fall back to an unrelated `agent` found
on `PATH`. Temporary Node.js download directories are removed on bootstrap exit,
including failed downloads. The bootstrap also validates `AGENT_CORE_PACKAGE`
as a single whitespace-free npm package spec before invoking npm, and rejects
values that start with `-` so package overrides cannot be interpreted as npm
options. The npm install step uses `--ignore-scripts` by default, with an
explicit `AGENT_CORE_NPM_RUN_SCRIPTS=1` opt-in for trusted source/git package
overrides that need lifecycle scripts; the README's git override example uses
that explicit opt-in.
`npm pack --dry-run --json` confirms that runtime package contents are still
only `README.md`, `dist/src`, `bin`, and `package.json`; the bootstrap script
is intended to be hosted by Web, not shipped inside the npm package.

`npm run smoke:package` installs the generated tarball into a temporary npm
prefix, runs `agent version`, runs installed `agent doctor` with a local
unreachable server URL to verify the packaged diagnostic JSON without depending
on external network availability, starts the foreground daemon with a temporary
`AGENT_CORE_HOME`, reads `/health`, uses the printed local control token to
read `/diagnostics`, verifies the installed `agent start --json` command
returns the existing daemon URL/token, verifies draft agent creation previews do not persist,
creates and deletes an unstarted local agent, verifies the installed daemon's
draft/current/proposed effective policy preview routes do not expose provider
environment variables, include canonical `fileAccess` resource grants, and do
not persist proposed policy or provider config projection changes,
mints a short-lived local grant, verifies the control-token
grant list returns metadata without token material, uses that grant to read
`/diagnostics`, verifies packaged `/pairing/confirm` rejects a missing
`authToken` with `400 bad_request` before reaching native core, verifies
packaged `/pairing/start` rejects a non-object body with `400 bad_request`,
revokes the grant, verifies the grant list marks it inactive, verifies the
revoked grant returns `401`,
uses the installed `agent rotate-token` command, verifies the old control token
returns `401`, verifies the new token can read `/diagnostics`, verifies the
packaged native sidecar profile through the reported `core` status, and stops
it with `agent stop`. That path passed with the packaged `agent` binary and
packaged macOS arm64 native sidecar. The same smoke path also passed with
`AGENT_CORE_NATIVE_BUILD_PROFILE=prod`, which stages the smaller non-dev
production-profile sidecar into the package before packing. After running the
prod smoke, `npm run build:native:dev` was run again to restore the local staged
sidecar to the default dev profile for ordinary development.
The smoke script redacts token-like strings in failure messages before printing
response bodies, so CI/package failures should not leak temporary local grant or
control-token values.
After the publish-bin fix, the default package smoke was rerun and still passed,
which verifies the installed `agent` command resolves from the packed tarball.
`agent doctor` on the current macOS machine reports `https://api.puffo.ai` as
reachable, and the detector suite now locks that URL as the default production
API when `AGENT_CORE_SERVER_URL` is unset.
The default and production-profile package smoke paths were rerun after the
installed `agent rotate-token` and `agent doctor` coverage was added and both
passed. After the production API base URL was confirmed as
`https://api.puffo.ai`, the native sidecar launcher was updated to forward that
default URL into prod sidecars unless `AGENT_CORE_SERVER_URL` overrides it. It
also forwards a default `AGENT_CORE_HOME`-scoped SQLite path unless
`AGENT_CORE_DATABASE_PATH` overrides it, leaving the server-issued auth token as
the remaining required production-profile input. The native sidecar can now
activate that token through `confirmPairing`; production `apple-keychain` builds
persist it in Keychain, fail the handoff if Keychain persistence fails, and
report Keychain read failures explicitly instead of treating them as a missing
token; dev/non-Keychain builds keep it only in sidecar memory. The package
smoke accepts either the no-token `missingConfig` state or an existing
Keychain-backed `pairing_required` state so local Keychain residue does not make
the smoke flaky. The focused sidecar test, default package smoke,
production-profile package smoke, native test suite, and full `npm test` run
all passed after that change.
After the bootstrap gained the user-local Node.js download fallback, the
focused package metadata test, full `npm test`, default package smoke, and
production-profile package smoke were rerun and passed; `npm run
build:native:dev` was run afterward to restore the local staged sidecar to the
default dev profile.
After the localhost API was tightened to reject provider-only starts without
`coreIdentity`, the focused API test, full `npm test`, default package smoke,
and production-profile package smoke were rerun and passed; `npm run
build:native:dev` was run afterward to restore the local staged sidecar to the
default dev profile.
Daemon resume was also tightened to refuse legacy persisted `running` agents
without `coreIdentity`; the focused daemon test, full `npm test`, default
package smoke, and production-profile package smoke were rerun after that
change.
The localhost API policy-update path was then tightened to reject actual
updates for running no-identity agents when the update would implicitly restart
the provider process; focused API tests, full `npm test`, default package
smoke, and production-profile package smoke passed after that change, and
`npm run build:native:dev` restored the local staged sidecar.
The create-agent path was also tightened to preflight native core readiness
before persisting an `operatorSlug` identity request, so production-profile
`pairing_required` native cores return a clear `400 bad_request` instead of
creating a local error agent and surfacing a generic server error.
The Web-signed MVP path was then added so `POST /agents` can accept an
already-signed/registered `coreIdentity` without calling native
`createAgentIdentity`; focused API and state-store tests passed after that
change.
The production-profile native health reason was then corrected to avoid saying
sidecar session wiring is unimplemented; `openAgentSession` now routes through
the production Rust core construction path, while backend PR #26 merge/deploy
for pairing plus backend PR #25 merge/deploy from `dev` for signer-id replay
and invite proof material remain blockers.
The macOS Web bootstrap was then tightened to refuse symlinked install prefixes,
Node install directories, and temporary download directories before any
directory creation, replacement, or npm install; the focused package metadata
test, full `npm test`, and default package smoke were rerun and passed after
that change.
The package repo hygiene was then tightened so generated native sidecars,
tarballs, local SQLite databases, and local `.env` files stay out of git while
`package.json`'s `files` allowlist still includes the staged native sidecar in
the npm tarball. `git check-ignore` verifies `/bin/`, generated package
tarballs, local DB files, env files, `dist/`, `node_modules/`, and Rust
`target/` directories are ignored without ignoring `crates/agent-native/src/bin`.
`npm pack --dry-run --json` was rechecked with a manifest assertion that
`bin/darwin/arm64/agent-native-sidecar` remains packaged and common generated
development artifacts are not packaged. The full test suite now also locks
this `.gitignore` boundary, including the `/bin/` root anchor that prevents the
rule from hiding Rust source files under `crates/agent-native/src/bin`.
The pack manifest assertion was then promoted into `npm run check:package`,
which runs `npm pack --dry-run --json` and fails if the tarball loses
`dist/src/cli/index.js`, loses the platform native sidecar, loses executable
modes, or includes source, tests, scripts, `node_modules`, Rust `target`,
package locks, local env files, local DB files including SQLite/DB sidecar
files such as `*.sqlite-wal` and `*.sqlite-shm`, or package tarballs. The
package metadata test now records that script name, and the focused metadata
test, `npm run check:package`, and default package smoke passed after adding
the verifier.
The publish gate now chains `node scripts/assert-release-profile.mjs && npm run
check:package`, so direct `npm publish` attempts first reject non-prod native
profiles and then verify the actual npm tarball manifest. The dev-profile
prepublish path was checked to fail before running the pack manifest verifier;
`AGENT_CORE_NATIVE_BUILD_PROFILE=prod npm run prepublishOnly` was checked to run
and pass the manifest verifier. `npm run build:native:dev` was run afterward to
restore the local staged sidecar to the default dev profile.
The package metadata tests now cover those lifecycle semantics with a fake
inner `npm`: dev-profile `prepublishOnly` must stop before `check:package`,
while prod-profile `prepublishOnly` must invoke `check:package`.
The real `npm publish --dry-run` lifecycle was also rechecked after this gate
change: the default/dev profile dry-run fails at the release-profile assertion,
and `AGENT_CORE_NATIVE_BUILD_PROFILE=prod npm publish --dry-run` runs the pack
manifest verifier, stages the prod sidecar, and completes the dry-run. `npm run
build:native:dev` was run afterward to restore the local staged sidecar.
The repository root now ignores local reference checkouts `/core/` and
`/hermes-agent/`, plus `.DS_Store`, so `git add .` does not accidentally stage
large auxiliary clones used during this investigation. The tracked dependency
remains `agent-core/core` through `.gitmodules`, and the existing tracked
`puffo-core-han-group` submodule is not hidden by this ignore rule. The full
`npm test`, `npm run test:native`, `npm run check:core-patch`, `npm run
check:package`, default package smoke, and production-profile package smoke
passed after that root ignore boundary was added; `npm run build:native:dev`
restored the local staged sidecar afterward.
The focused sidecar test, `cargo test --manifest-path
crates/agent-native/Cargo.toml`, full `npm test`, default package smoke, and
production-profile package smoke passed after that wording fix. The
`AGENT_CORE_NATIVE_BUILD_PROFILE=prod npm publish --dry-run` release gate also
passed, and `npm run build:native:dev` restored the local staged sidecar.
The production native status payloads now include `blockedBy` and `nextAction`
for Web/product integration: missing local config reports the exact missing
input such as `AGENT_CORE_AUTH_TOKEN`, and contract-blocked production states
name backend PR #26 merge/deploy for pairing plus backend PR #25 merge/deploy
from `dev` for space/invite sync explicitly.
The focused sidecar test, native crate test, full
`npm test`, default package smoke, and production-profile package smoke passed
after adding those fields; `npm run build:native:dev` restored the local staged
sidecar.
The package smoke now also asserts those production status fields on the packed
binary path, so a release tarball cannot silently drop the Web-facing blocker
metadata.
After updating the production status wording to name backend PR #25
merge/deploy from `dev` explicitly, the focused sidecar test, full `npm test`, native
crate test suite, default package smoke, production-profile package smoke,
`npm run check:package`, and `git diff --check` passed. `npm run
build:native:dev` restored the staged native sidecar to the default dev profile
after the production-profile smoke.
The package smoke assertion was then tightened so packaged production sidecars
must expose both `backend_pairing_contract` and `space_invite_sync_contract`,
must mention backend PR #25 from `dev` in the production reason, and must
mention backend PR #25 from `dev` plus production agent identity publication in
`nextAction`. The focused package metadata test, `npm run check:package`,
default package smoke, and production-profile package smoke passed after that
change; `npm run build:native:dev` restored the staged sidecar afterward.
After PR #25 was retargeted from `main` to `dev`, the runtime status text and
package smoke assertion were tightened again to require `from dev`; the focused
sidecar test, full `npm test` suite with 171 passing tests, `npm run
check:package`, and production-profile package smoke passed, then `npm run
build:native:dev` restored the staged sidecar.
The production release dry-run gate was rerun after the smoke assertion
tightening: `AGENT_CORE_NATIVE_BUILD_PROFILE=prod npm publish --dry-run`
passed, including `prepublishOnly`, `npm run check:package`, `prepack`, and
the production native sidecar staging path. `npm run build:native:dev` restored
the staged sidecar afterward.
The package README was also tightened to avoid Markdown links into repository
`docs/` files that are not shipped in the npm package. The package metadata
test now rejects `README.md` links to `../docs/`, and the focused package
metadata test plus `npm run check:package` passed after that guard was added.
The full `npm test` suite was then rerun and passed with 171 tests, covering
that guard through the normal package metadata suite.
The same README guard was then promoted into `scripts/check-pack-manifest.mjs`,
so the `prepublishOnly` release gate also rejects published README links to
unshipped `../docs/` paths. `npm run check:package`, the focused package
metadata test, and `AGENT_CORE_NATIVE_BUILD_PROFILE=prod npm run
prepublishOnly` passed after that change.
The root `.gitmodules` file now also records the pre-existing
`puffo-core-han-group` gitlink, not only `agent-core/core`, so `git submodule
status` no longer fails with a missing submodule mapping. Both submodule URLs
use HTTPS rather than SSH to avoid environments where GitHub SSH on port 22 is
blocked. The checked-out `puffo-core-han-group` worktree now points at Web PR
#52 commit `165c6a0...` for the Web-signed MVP handoff. The package metadata
suite now locks the root `.gitmodules` behavior, including the HTTPS URLs,
absence of `git@github.com:` submodule URLs, and the recorded
`agent-core/core` gitlink matching the checked-out core revision. It also
checks that the exported core patch records an explicit upstream base revision;
the focused metadata suite passed with 25 tests after adding those guards.
The same blocker metadata was then made consistent for the default dev sidecar
pairing path, the explicit `UnavailableCoreNative` fallback, and API
`/diagnostics` native-core exception handling. Focused API/sidecar tests,
native crate tests, full `npm test`, default package smoke, and
production-profile package smoke passed after that consistency pass; the staged
native sidecar was restored to dev afterward.

The `core` client changes were additionally checked with
`cargo build -p puffo-client`, `cargo test -p puffo-client`, and
`cargo test -p puffo-client --features dev-tools`. The default run now passes
the release/default-profile tests without compiling dev-only integration tests.
The feature-enabled test run passes the client contract/scenario suite,
including `public_api_supports_operator_bound_agent_identity_creation` and
`blocking_http_transport_sends_json_and_decodes_json_response`.
The exported upstream patch was checked both as a reverse apply against the
current `agent-core/core` PR branch tree and as a forward `git apply --check`
against a clean detached worktree at the recorded upstream patch base.
That check is now captured as `npm run check:core-patch`, which creates and
removes its own temporary clean worktree instead of requiring reviewers to copy
the manual worktree commands from the handoff doc.
`npm run export:core-patch` now regenerates the exported handoff patch from the
submodule diff against the recorded upstream patch base and immediately runs
the same patch verifier; the generated patch was checked by comparing its SHA
before and after export. After aligning the Rust HTTP provider's
`/messages/pending` and `/messages/ack` route shapes to the pulled
`puffo-server/main` backend, and after adding Rust-side agent identity
publication against `POST /agents` plus `POST /certs/slug_binding`, and after
mapping current backend space reads plus backend PR #25 invite proof rows, the
exported patch is 106,416 bytes with SHA-256
`097264a775d1b3884e6fccd8103cc8c45e316399b65785a142a920be0f36ba17`.
Both core patch scripts now fail early with an explicit
`git submodule update --init agent-core/core` hint when the submodule directory
is missing, which makes fresh-checkout setup failures easier to diagnose before
reviewers hit lower-level git errors. Package metadata tests now exercise that
fresh-checkout missing-submodule path for both `check:core-patch` and
`export:core-patch`.
After the core changes were committed to `feature/agent-core-native-bridge` and
opened as PR #18, the parent `agent-core/core` gitlink was moved to the PR
commit `ece389a...` and the patch verifier was updated to use
`docs/patches/agent-core-core-upstream.base` as its stable base instead of the
current parent gitlink. That keeps `npm run check:core-patch` useful both for
fresh-clone reproducibility and for reviewing the upstream patch against its
original base. After that change, full `npm test` passed with 173 tests,
`npm run check:package` passed with a 228-file tarball manifest, `npm run
check:core-patch` passed against the explicit base, and `git diff --check`
passed.
After adding Web-signed `coreIdentity` attachment, full `npm test` was rerun and
passed with 174 tests. The current completion audit then reran `npm run
test:native`, `npm run smoke:package`,
`AGENT_CORE_NATIVE_BUILD_PROFILE=prod npm run smoke:package`, `npm pack
--dry-run`, `AGENT_CORE_NATIVE_BUILD_PROFILE=prod npm publish --dry-run`,
`npm run check:package`, `npm run check:core-patch`, and `git diff --check`;
all passed. `npm run build:native:dev` restored the staged native sidecar to
the default dev profile after the production-profile package checks.
After adding `ServerPairingGateway`, full `npm test` passed with 175 tests,
`npm run test:native` passed, `npm run check:package` passed with a 231-file
tarball manifest, `npm run check:core-patch` passed, default and
production-profile package smoke passed, the prod-profile publish dry run
passed, and `npm run build:native:dev` restored the staged sidecar afterward.
After tightening server-requested local Web grant TTL handling, full `npm test`
passed with 176 tests, `npm run check:package` passed with the 231-file tarball
manifest, `npm run check:core-patch` passed, `git diff --check` passed, and
the default/prod package smoke paths passed when run sequentially. These smoke
paths both mutate the staged native sidecar, so they should not run in parallel
in the same working tree.
After adding runtime validation for backend pairing response shapes, full
`npm test` passed with 177 tests, `npm run check:package` passed with the
231-file tarball manifest, `npm run check:core-patch` passed, and `git diff
--check` passed.
After adding explicit Web-signed `start: true` failure coverage, full
`npm test` passed with 178 tests and `npm run check:package` still passed with
the 231-file tarball manifest. This verifies that a Web-signed agent does not
remain provider-only running when native `openAgentSession` fails.
After clarifying the Web-signed MVP handoff and native pairing responsibility,
full `npm test` passed again with 178 tests, `npm run check:package` passed
with the 231-file tarball manifest, `npm run check:core-patch` passed, `git
diff --check` passed, and a stale-text scan found no remaining legacy
pairing-contract wording in `agent-core` or `docs`.

Current PR state rechecked on 2026-05-07:

```text
core PR #18: OPEN, non-draft, MERGEABLE
  branch: feature/agent-core-native-bridge -> main
  head: ece389a12da5ce3745a213d54a0d55b1b56e3729
backend PR #25: OPEN, non-draft, MERGEABLE, CI checks successful
  branch: feature/space-event-signer-ids -> dev
  head: 7e971f775f225ebb41b99bf9c540084c6be2aca3
backend PR #26: OPEN, non-draft, MERGEABLE
  branch: feature/agent-core-pairing-contract -> dev
  head: 50982d550c761530b9ea02ba4ae4c9e448f998c6
```

The backend PR #26 diff was also checked for contract drift. Its response
structs are still camelCase and match `ServerPairingGateway`: start returns
`pairingId`, `userCode`, `confirmUrl`, `expiresAt`, and `pollAfterMs`; poll
returns `status`, `expiresAt`, optional `pollAfterMs`, `operatorSlug`,
`accountId`, one-time `authToken`, `operatorBootstrap`, and
`localWebGrant.mode = "daemon_mints"` with `ttlMs`.

After enforcing `declaredOperatorPublicKey` for Web-signed `coreIdentity`,
runtime-supplied `coreIdentity`, and native `createAgentIdentity` results,
focused API/state tests passed with 68 tests, full `npm test` passed with 180
tests, `npm run check:package` passed with the 231-file tarball manifest,
`npm run check:core-patch` passed, and `git diff --check` passed.
After making `CoreAgentIdentity.declaredOperatorPublicKey` required at the
TypeScript type boundary and dropping incomplete legacy persisted
`coreIdentity` metadata during state normalization, focused API/state/daemon
tests passed with 75 tests, full `npm test` passed with 181 tests,
`npm run check:package` passed with the 231-file tarball manifest,
`npm run check:core-patch` passed, and `git diff --check` passed.
After tightening the native bridge `createAgentIdentity` TypeScript return
contract and applying the same supplied-identity validation to
`previewCreateAgent`, focused API/state tests passed with 70 tests, full
`npm test` passed with 182 tests, `npm run check:package` passed with the
231-file tarball manifest, `npm run check:core-patch` passed, and
`git diff --check` passed.
After normalizing missing or invalid `coreIdentity.source` to `web_signed` at
runtime and StateStore boundaries, focused API/state tests passed with 72 tests,
full `npm test` passed with 184 tests, `npm run check:package` passed with the
231-file tarball manifest, `npm run check:core-patch` passed, and
`git diff --check` passed.
After sharing core slug validation across API, runtime, and StateStore
boundaries, focused API/state tests passed with 75 tests, full `npm test`
passed with 187 tests, `npm run check:package` passed with the 234-file
tarball manifest, `npm run check:core-patch` passed, and `git diff --check`
passed.
After applying the same core slug validation to confirmed server pairing
responses and rejecting mismatched `operatorBootstrap.operatorSlug` before
native `confirmPairing`, focused API tests passed with 26 tests, full `npm
test` passed with 188 tests, `npm run check:package` passed with the 234-file
tarball manifest, `npm run check:core-patch` passed, and `git diff --check`
passed.
After tightening the backend pairing start/poll response envelope with bounded
`pairingId`, `userCode`, `confirmUrl`, `expiresAt`, and `pollAfterMs`
validation, focused API tests passed with 27 tests, full `npm test` passed with
189 tests, `npm run check:package` passed with the 237-file tarball manifest,
`npm run check:core-patch` passed, and `git diff --check` passed.
After making `localApiOrigin` daemon-derived only and rejecting spoofed
`localApiOrigin` fields in local `/pairing/start` bodies, focused API tests
passed with 27 tests, full `npm test` passed with 189 tests,
`npm run check:package` passed with the 237-file tarball manifest,
`npm run check:core-patch` passed, and `git diff --check` passed.
After validating supplied `pairingPublicNonce` as an unpadded base64url string
from 32 to 128 characters at both the API and gateway boundaries, focused API
tests passed with 27 tests, full `npm test` passed with 189 tests,
`npm run check:package` passed with the 240-file tarball manifest,
`npm run check:core-patch` passed, `npm run smoke:package` passed against
packaged `agent 0.1.0`, and `git diff --check` passed.
After stripping unsupported `coreIdentity` fields at runtime preview and
StateStore save boundaries, focused API/state tests passed with 79 tests, full
`npm test` passed with 191 tests, `npm run check:package` passed with the
240-file tarball manifest, `npm run check:core-patch` passed,
`npm run smoke:package` passed against packaged `agent 0.1.0`, and
`git diff --check` passed.
After applying the same safe pairing id validation to manual
`POST /pairing/confirm` bodies that the poll route already uses, focused API
tests passed with 27 tests, full `npm test` passed with 191 tests,
`npm run check:package` passed with the 240-file tarball manifest,
`npm run check:core-patch` passed, `npm run smoke:package` passed against
packaged `agent 0.1.0`, and `git diff --check` passed.
After trimming and bounding pairing `authToken` values at 16 KiB before manual
confirm or server-confirmed poll responses can forward them to native core,
focused API tests passed with 27 tests, full `npm test` passed with 191 tests,
`npm run check:package` passed with the 243-file tarball manifest,
`npm run check:core-patch` passed, `npm run smoke:package` passed against
packaged `agent 0.1.0`, and `git diff --check` passed.
After applying the same pairing confirm validation at the `SidecarCoreNative`
boundary before spawning or messaging the Rust sidecar, focused sidecar tests
passed with 13 tests, full `npm test` passed with 192 tests,
`npm run check:package` passed with the 243-file tarball manifest,
`npm run check:core-patch` passed, `npm run smoke:package` passed against
packaged `agent 0.1.0`, and `git diff --check` passed.
After consolidating pairing confirm normalization into a shared platform helper
and enforcing it at API, `ServerPairingGateway`, and `SidecarCoreNative`
boundaries, focused API/sidecar tests passed with 41 tests, full `npm test`
passed with 193 tests, `npm run check:package` passed with the 246-file
tarball manifest, `npm run check:core-patch` passed, `npm run smoke:package`
passed against packaged `agent 0.1.0`, and `git diff --check` passed.
After rejecting backend pairing start `confirmUrl` values that embed username or
password credentials before returning them to Web, focused API tests passed with
28 tests, full `npm test` passed with 193 tests, `npm run check:package`
passed with the 246-file tarball manifest, `npm run check:core-patch` passed,
`npm run smoke:package` passed against packaged `agent 0.1.0`, and
`git diff --check` passed.
After tightening backend pairing start `confirmUrl` validation to allow HTTPS
or loopback HTTP only, focused API tests passed with 28 tests, full `npm test`
passed with 193 tests, `npm run check:package` passed with the 246-file
tarball manifest, `npm run check:core-patch` passed, `npm run smoke:package`
passed against packaged `agent 0.1.0`, and `git diff --check` passed.
After constraining confirmed-pairing `operatorBootstrap` to known bounded
shapes and rejecting unsupported fields such as accidental secret-key material,
focused API tests passed with 28 tests, full `npm test` passed with 193 tests,
`npm run check:package` passed with the 246-file tarball manifest,
`npm run check:core-patch` passed, `npm run smoke:package` passed against
packaged `agent 0.1.0`, and `git diff --check` passed.
After making server-requested `localWebGrant` hints visible only when the
one-time `authToken` poll actually mints a local grant, focused API tests passed
with 28 tests, full `npm test` passed with 193 tests, `npm run check:package`
passed with the 246-file tarball manifest, `npm run check:core-patch` passed,
`npm run smoke:package` passed against packaged `agent 0.1.0`, and
`git diff --check` passed.
After validating native `startPairing` fallback input at the
`SidecarCoreNative` boundary and rejecting spoofed `localApiOrigin` or malformed
`pairingPublicNonce` values before native sidecar spawn, focused sidecar tests
passed with 14 tests, full `npm test` passed with 194 tests,
`npm run check:package` passed with the 246-file tarball manifest,
`npm run check:core-patch` passed, `npm run smoke:package` passed against
packaged `agent 0.1.0`, and `git diff --check` passed.
After bounding confirmed-pairing `accountId` values before returning them to Web
or forwarding the paired token to native core, focused API tests passed with 28
tests, full `npm test` passed with 194 tests, `npm run check:package` passed
with the 246-file tarball manifest, `npm run check:core-patch` passed,
`npm run smoke:package` passed against packaged `agent 0.1.0`, and
`git diff --check` passed.
After adding a 64 KiB bound while streaming backend pairing JSON responses
before parsing them, focused API tests passed with 28 tests, full `npm test`
passed with 194 tests, `npm run check:package` passed with the 246-file
tarball manifest, `npm run check:core-patch` passed, `npm run smoke:package`
passed against packaged `agent 0.1.0`, and `git diff --check` passed.
After disabling redirect following for backend pairing requests and asserting
redirected pairing start responses fail closed without reaching the redirected
route, focused API tests passed with 29 tests, full `npm test` passed with 195
tests, `npm run check:package` passed with the 246-file tarball manifest,
`npm run check:core-patch` passed, `npm run smoke:package` passed against
packaged `agent 0.1.0`, and `git diff --check` passed.
After requiring JSON-compatible `Content-Type` headers for successful backend
pairing responses and validating pairing server base URLs before requests
against HTTPS-or-loopback-HTTP rules, focused API tests passed with 30 tests,
full `npm test` passed with 196 tests, `npm run check:package` passed with the
246-file tarball manifest, `npm run check:core-patch` passed,
`npm run smoke:package` passed against packaged `agent 0.1.0`, and
`git diff --check` passed.
After tightening the backend poll response state machine so pending, expired,
and canceled responses reject confirmed-only fields such as `authToken`,
`operatorBootstrap`, and `localWebGrant` before reaching native core or Web,
focused API tests passed with 30 tests, full `npm test` passed with 196 tests,
`npm run check:package` passed with the 246-file tarball manifest,
`npm run check:core-patch` passed, `npm run smoke:package` passed against
packaged `agent 0.1.0`, and `git diff --check` passed.
After parsing backend pairing start and confirmed poll responses as strict
top-level schemas and rejecting unexpected fields such as accidental secret-key
material, focused API tests passed with 30 tests, full `npm test` passed with
196 tests, `npm run check:package` passed with the 246-file tarball manifest,
`npm run check:core-patch` passed, `npm run smoke:package` passed against
packaged `agent 0.1.0`, and `git diff --check` passed.
After stripping `operatorBootstrap.payload` from localhost confirmed-pairing
responses and returning only the Web-safe `kind`/`operatorSlug` summary,
focused API tests passed with 30 tests, full `npm test` passed with 196 tests,
`npm run check:package` passed with the 246-file tarball manifest,
`npm run check:core-patch` passed, `npm run smoke:package` passed against
packaged `agent 0.1.0`, and `git diff --check` passed.
After adding Rust-side `deny_unknown_fields` validation to native sidecar RPC
request/parameter structs so direct sidecar payloads cannot silently carry
unexpected fields, `npm run test:native` passed across default, `dev-tools`,
and `apple-keychain` check profiles; full `npm test` passed with 196 tests,
`npm run check:package` passed with the 246-file tarball manifest,
`npm run check:core-patch` passed, `npm run smoke:package` passed against
packaged `agent 0.1.0`, and `git diff --check` passed.
After rejecting unsupported fields on local/native `pairing/confirm` input so
only `authToken` and optional safe `pairingId` can reach native core, focused
API/sidecar tests passed with 44 tests, full `npm test` passed with 196 tests,
`npm run test:native` passed across default, `dev-tools`, and `apple-keychain`
check profiles, `npm run check:package` passed with the 246-file tarball
manifest, `npm run check:core-patch` passed, `npm run smoke:package` passed
against packaged `agent 0.1.0`, and `git diff --check` passed.
After making local grant creation reject unsupported fields and requiring local
control-token rotation requests to have an empty body, focused API tests passed
with 30 tests, full `npm test` passed with 196 tests, `npm run test:native`
passed across default, `dev-tools`, and `apple-keychain` check profiles,
`npm run check:package` passed with the 246-file tarball manifest,
`npm run check:core-patch` passed, `npm run smoke:package` passed against
packaged `agent 0.1.0`, and `git diff --check` passed.
After making agent creation, agent preview, policy updates, and nested
`fileAccess` objects reject unsupported fields before they can affect persisted
policy or sandbox inputs, focused API tests passed with 30 tests, full
`npm test` passed with 196 tests, `npm run test:native` passed across default,
`dev-tools`, and `apple-keychain` check profiles, `npm run check:package`
passed with the 246-file tarball manifest, `npm run check:core-patch` passed,
`npm run smoke:package` passed against packaged `agent 0.1.0`, and
`git diff --check` passed.
After requiring agent action routes `start`, `stop`, `restart`,
`reset-session`, and `recheck` to receive empty request bodies, focused API
tests passed with 30 tests, full `npm test` passed with 196 tests,
`npm run test:native` passed across default, `dev-tools`, and
`apple-keychain` check profiles, `npm run check:package` passed with the
246-file tarball manifest, `npm run check:core-patch` passed,
`npm run smoke:package` passed against packaged `agent 0.1.0`, and
`git diff --check` passed.
After requiring destructive DELETE routes for local agents and local grants to
receive empty request bodies before deleting state or revoking credentials,
focused API tests passed with 30 tests, full `npm test` passed with 196 tests,
`npm run test:native` passed across default, `dev-tools`, and
`apple-keychain` check profiles, `npm run check:package` passed with the
246-file tarball manifest, `npm run check:core-patch` passed,
`npm run smoke:package` passed against packaged `agent 0.1.0`, and
`git diff --check` passed.
After making the dev-only message injection route accept only `body` and an
optional lowercase `senderSlug`, focused API tests passed with 30 tests, full
`npm test` passed with 196 tests, `npm run test:native` passed across default,
`dev-tools`, and `apple-keychain` check profiles, `npm run check:package`
passed with the 246-file tarball manifest, `npm run check:core-patch` passed,
`npm run smoke:package` passed against packaged `agent 0.1.0`, and
`git diff --check` passed.
After making local `pairing/start` accept only `pairingPublicNonce` and applying
the same strict input boundary to direct `ServerPairingGateway.startPairing`
calls, focused API tests passed with 30 tests, full `npm test` passed with 196
tests, `npm run test:native` passed across default, `dev-tools`, and
`apple-keychain` check profiles, `npm run check:package` passed with the
246-file tarball manifest, `npm run check:core-patch` passed,
`npm run smoke:package` passed against packaged `agent 0.1.0`, and
`git diff --check` passed.
After pinning `publishConfig.registry` to `https://registry.npmjs.org/` so
local npm registry mirrors cannot redirect release publishes, focused package
metadata tests passed with 25 tests and
`AGENT_CORE_NATIVE_BUILD_PROFILE=prod npm publish --dry-run` published to the
pinned npmjs registry in dry-run mode. Full regression then passed with
`npm test` at 196 tests, `npm run test:native` across default, `dev-tools`, and
`apple-keychain` check profiles, `npm run check:package`, `npm run
check:core-patch`, dev and prod-profile `npm run smoke:package`,
`AGENT_CORE_NATIVE_BUILD_PROFILE=prod npm publish --dry-run`, and
`git diff --check`; the staged native sidecar was restored to the dev profile
after the prod dry-run.
Current npm registry lookup shows `agent-core` is already occupied on npmjs by
`agent-core@0.0.1-security`, so the package was later moved to the scoped npm
name `@puffo-ai/agent-core` while keeping the installed CLI binary as `agent`.
After adding `bootstrap-macos.sh` CLI options for Web-hosted one-line installs,
including `--package <spec>` and `--run-scripts`, Web can use
`curl ... | bash -s -- --package <npm-spec>` if the release package name changes
or a temporary package source is needed while keeping the installed binary name
as `agent`. Focused package metadata/bootstrap tests passed with 26 tests, full
`npm test` passed with 197 tests, `npm run test:native`, `npm run
check:package`, `npm run check:core-patch`, dev and prod-profile
`npm run smoke:package`, `AGENT_CORE_NATIVE_BUILD_PROFILE=prod npm publish
--dry-run`, and `git diff --check` all passed; the staged native sidecar was
restored to the dev profile after the prod dry-run.
After making percent-decoding failures in `GET /pairing/:pairingId` return
`400 bad_request` instead of falling through to a generic internal error, API
tests cover malformed percent encoding and encoded path traversal before any
backend poll is attempted. Focused API tests passed with 30 tests, full
`npm test` passed with 197 tests, `npm run test:native`, `npm run
check:package`, `npm run check:core-patch`, dev and prod-profile
`npm run smoke:package`, `AGENT_CORE_NATIVE_BUILD_PROFILE=prod npm publish
--dry-run`, and `git diff --check` all passed; the staged native sidecar was
restored to the dev profile after the prod dry-run.
After making `bootstrap-macos.sh` fail closed before local Node/npm setup when
no explicit package source is provided, the Web-hosted install path could not
accidentally install the occupied `agent-core@0.0.1-security` npm package or
download fallback Node first. That temporary guard has been removed now that
the package default is the scoped `@puffo-ai/agent-core` npm package.
Focused package metadata/bootstrap tests passed with 27 tests, including a fake
`node`/`curl`/`npm` guard for the previous default no-package path; full
`npm test` passed with 198 tests, `npm run test:native`, `npm run
check:package`, `npm run check:core-patch`, dev and prod-profile
`npm run smoke:package`, `AGENT_CORE_NATIVE_BUILD_PROFILE=prod npm publish
--dry-run`, and `git diff --check` all passed; the staged native sidecar was
restored to the dev profile after the prod dry-run.
After making Web account-context management headers fail closed,
`X-Agent-Core-Account-Id` is bounded to 256 characters and
`X-Agent-Core-Operator-Slug` must be a lowercase core slug; malformed headers
return `400 bad_request` instead of silently dropping the account context, while
valid mismatched headers still return `409 account_mismatch`. Focused API tests
passed with 30 tests, full `npm test` passed with 198 tests, `npm run
test:native`, `npm run check:package`, `npm run check:core-patch`, dev and
prod-profile `npm run smoke:package`,
`AGENT_CORE_NATIVE_BUILD_PROFILE=prod npm publish --dry-run`, and
`git diff --check` all passed; the staged native sidecar was restored to the dev
profile after the prod dry-run.
After making `/configuration` fail closed on setup-probe query shape, only
`accountId` and `operatorSlug` are accepted, each at most once. Malformed
account ids, non-lowercase operator slugs, duplicate query fields, and typoed
fields now return `400 bad_request` before Web can treat the daemon as
configured for the wrong current account. Focused API build/tests passed with
30 tests.
After applying the same fail-closed query rule to policy previews and log
tails, `/agents/:id/policy` accepts only a single `preview` query parameter and
`/agents/:id/logs` accepts only a single `maxLines` query parameter. Unknown or
duplicate query fields now return `400 bad_request` instead of being silently
ignored. Focused API build/tests passed with 30 tests, full `npm test` passed
with 198 tests, and `git diff --check` passed.
After tightening management JSON body validation, local grant creation and
empty-body management actions now reject arrays, `null`, and primitive JSON
values with `400 bad_request` instead of accepting `[]` as empty or surfacing an
internal error. The same JSON-object guard covers dev-only message injection,
so its strict schema cannot turn `null` into an internal error. Focused API
build/tests passed with 30 tests, full `npm test` passed with 198 tests, and
`npm run smoke:package` plus `git diff --check` passed.
After mirroring the same object-body guard inside the native bridge helpers,
direct `SidecarCoreNative.startPairing` and `confirmPairing` calls reject
arrays or `null` before spawning the native sidecar. Focused sidecar bridge
tests passed with 14 tests, full `npm test` passed with 198 tests, and
`git diff --check` passed.
After pushing native bridge validation down to agent identity/session entry
points, direct `SidecarCoreNative.createAgentIdentity` and `openAgentSession`
calls reject malformed operator/agent slugs and non-object session inputs
before spawning the native sidecar. Focused sidecar bridge tests passed with 15
tests, full `npm test` passed with 199 tests, and `git diff --check` passed.
After tightening native bridge message/session RPCs, `syncOnce`,
`processPendingMessages`, `snapshot`, `closeSession`, `sendChannelReply`,
`sendDirectReply`, and `devInjectChannelMessage` now validate non-empty session
handles, object payloads, and recipient/dev slugs before native RPC. Focused
sidecar bridge tests passed with 16 tests, full `npm test` passed with 200
tests, and `git diff --check` passed.
After mirroring the same pre-native validation in the process-per-command CLI
fallback bridge, `CliCoreNative` now validates agent identity slugs, session
input objects, message payload objects, recipient slugs, and non-empty session
handles before invoking `cargo run`. Focused CLI fallback tests passed with 2
tests, full `npm test` passed with 202 tests, and `git diff --check` passed.
After validating native bridge response shapes, both the persistent sidecar and
CLI fallback now reject missing session handles, message ids, pending-message
arrays, and agent identity public-key material instead of passing `undefined`
up to `RuntimeManager`. Focused sidecar response-shape tests passed with 17
tests, focused CLI fallback response-shape tests passed with 3 tests, full
`npm test` passed with 204 tests, `npm run smoke:package` passed, and
`git diff --check` passed.
After adding status-shape validation for native health and pairing responses,
the persistent sidecar now requires health responses to include a boolean
`connected` and a known device `status`, and pairing responses to include a
known pairing `status`; the CLI fallback now requires native health stdout to
include a boolean `ok` and non-empty `mode`. Extra diagnostic fields such as
test-only `env` are still preserved. Focused native bridge tests passed with
20 tests, full `npm test` passed with 204 tests, `npm run smoke:package`
passed, and `git diff --check` passed.
After tightening the persistent sidecar JSONL RPC envelope, responses now
require a safe integer `id` and boolean `ok`; malformed `ok` values reject the
pending request instead of being treated as truthy success. Focused sidecar
tests passed with 18 tests, full `npm test` passed with 205 tests, `npm run
smoke:package` passed, and `git diff --check` passed.
After aligning the production native `createAgentIdentity` response with the
TypeScript bridge contract, the Rust sidecar now reads the published local
agent IdentityCert's `declared_operator_public_key` from the Rust store and
returns it as `declaredOperatorPublicKey`; if that public operator anchor is
missing, production identity creation fails before Node persists incomplete
metadata. `npm run test:native` passed, including the `apple-keychain`
production sidecar check; full `npm test` passed with 205 tests, `npm run
check:package` passed with the 246-file tarball manifest, `npm run
smoke:package` passed, `git diff --check` passed, and no root package tarballs
were left behind.
After making server-confirmed pairing start/poll public loopback routes,
fresh Web clients can now call `POST /pairing/start` and
`GET /pairing/:pairingId` before they have a local grant; direct
`POST /pairing/confirm` remains token-protected because it accepts a
server-issued native auth token. The route was then tightened so browser
start/poll callers must come from a trusted Origin before a confirmed poll can
return a local grant; non-browser local clients without an `Origin` header still
work. Focused API tests passed with 30 tests, full `npm test` passed with 205
tests, `npm run check:package` passed with the 246-file tarball manifest, and
`npm run smoke:package` passed; `git diff --check` passed and no root package
tarballs were left behind.
After adding legacy read-only `/v1/info` discovery compatibility, existing Web
builds that only probe the old bridge discovery route can detect that the new
`agent-core` daemon is running without re-enabling the old `/v1/pair` or
secret-bundle `/v1/agents` protocol. Focused API tests passed with 30 tests,
full `npm test` passed with 205 tests, `npm run check:package` passed with the
246-file package manifest, `npm run smoke:package` passed, root
`git diff --check` passed, and no generated `.tgz` package artifacts remained
under `agent-core/`.
After adding explicit regression coverage for rejecting the old Web bridge's
top-level `identity_bundle` secret handoff on new `POST /agents`, focused API
tests passed with 30 tests, full `npm test` passed with 205 tests,
`npm run check:package` passed with the 246-file package manifest,
`npm run smoke:package` passed, root `git diff --check` passed, and no
generated `.tgz` package artifacts remained under `agent-core/`.
After adding explicit regression coverage that old Web bridge `x-puffo-*`
signed local headers do not authenticate the new local management API, focused
API tests passed with 30 tests, full `npm test` passed with 205 tests,
`npm run check:package` passed with the 246-file package manifest,
`npm run smoke:package` passed when rerun sequentially after `npm test`, root
`git diff --check` passed, and no generated `.tgz` package artifacts remained
under `agent-core/`.
After adding explicit regression coverage that legacy `/v1/pair` and
secret-bundle `/v1/agents` remain unsupported while `/v1/info` stays available
for discovery, focused API tests passed with 30 tests, full `npm test` passed
with 205 tests, `npm run check:package` passed with the 246-file package
manifest, `npm run smoke:package` passed, root `git diff --check` passed, and
no generated `.tgz` package artifacts remained under `agent-core/`.
After reserving `coreIdentity.source = "native"` for the Rust native creation
path, local API callers can only omit `source` or send `web_signed` on create
and preview; direct runtime supplied identities are defensively coerced to
`web_signed`. Focused API/state tests passed with 82 tests, full `npm test`
passed with 205 tests,
`npm run check:package` passed with the 246-file package manifest,
`npm run smoke:package` passed, root `git diff --check` passed, and no
generated `.tgz` package artifacts remained under `agent-core/`.
After adding the shared `declaredOperatorPublicKey` normalizer and enforcing
the same 4096-character limit across local API, RuntimeManager, StateStore,
SidecarCoreNative, and CliCoreNative, focused API/state/sidecar/CLI tests
passed with 104 tests, full `npm test` passed with 206 tests,
`npm run check:package` passed with the 249-file package manifest,
`npm run smoke:package` passed when rerun sequentially after `npm test`, root
`git diff --check` passed, and no generated `.tgz` package artifacts remained
under `agent-core/`.
After adding the Web non-UI `agent-core` client/provision helper for the
Web-signed MVP handoff, the changes were committed to
`puffo-core-han-group` branch `feature/agent-core-web-signed-mvp` at
`165c6a0...` and opened as PR
https://github.com/puffo-ai/puffo-core-han-group/pull/52 against `main`.
The PR is open, mergeable, and its GitHub `Check & Test` plus Web
`Type-check + build` checks completed successfully. Focused Web tests passed
locally with 9 tests:
`npm test -- tests/agent-core-client.test.ts tests/agent-core-provision.test.ts`
from `puffo-core-han-group/client/web`. The tests cover public discovery without
local auth headers, bearer/account management headers, confirmed pairing
`localGrant` installation for subsequent management calls, pending polls not
overwriting existing auth, non-confirmed pairing responses ignoring unexpected
local grants, control-token exchange to a scoped local grant, and metadata-only
Web-signed agent handoff without secret bundles. After generating
the ignored wasm package with
`npx wasm-pack build ./wasm-v2 --target web --out-dir pkg --release`, full Web
`npm run typecheck` passed and full Web `npm test` passed with 27 files and
363 tests. The Web test setup now replaces Node 25's incomplete experimental
storage object with an in-memory Storage polyfill when needed, and `SignupPage`
clears its invite-code debounce timer on unmount so the full suite does not
leave an unhandled state update after teardown.
After switching the npm package to scoped `@puffo-ai/agent-core`, the default
macOS bootstrap package source no longer points at the occupied unscoped
`agent-core` npm package. The installed binary name remains `agent`, the
bootstrap default is `@puffo-ai/agent-core`, and `publishConfig.access=public`
keeps the scoped release public. Focused package/bootstrap tests passed with 27
tests, `npm run check:package` passed with the 249-file package manifest,
`npm run smoke:package` passed, full `npm test` passed with 206 tests, and
`AGENT_CORE_NATIVE_BUILD_PROFILE=prod npm publish --dry-run` confirmed dry-run
publishing to `https://registry.npmjs.org/` with public access. The staged
native sidecar was restored to the dev profile with `npm run build:native:dev`.
`npm run test:native` then passed the default Rust tests, `dev-tools` Rust
tests, and `apple-keychain` sidecar `cargo check`; `npm run check:core-patch`
also passed against `docs/patches/agent-core-core-upstream.patch`. A registry
lookup for `@puffo-ai/agent-core` on 2026-05-07 returned npm 404, which means
the scoped package is not yet published from this environment; actual release
still requires npm org access.
The parent implementation has been assembled against the existing
`puffo-ai/puffoagent` runtime repository on branch
`feature/agent-core-local-mvp`. `npm whoami
--registry=https://registry.npmjs.org/` returned `ENEEDAUTH`, so publishing
`@puffo-ai/agent-core` cannot be performed from this machine without npm
login/org access.

## Completion Decision

The local CLI agent core MVP is implemented far enough for Web integration
against localhost, provider detection, agent lifecycle management, macOS policy
experiments, and dev encrypted-message smoke tests.

The full objective is not production-complete until:

1. PR #18 with the local `core` API changes is merged into `puffo-ai/core`,
   and the parent submodule pointer is updated from the PR branch commit to the
   merged revision;
2. backend PR #25 with signer-id replay plus invitation proof material is
   merged to `dev` and deployed from that isolated branch;
3. backend PR #26 with the local daemon pairing contract is merged to `dev` and
   deployed from that isolated branch;
4. for the native signing path, server-confirmed pairing provisions or restores
   the operator identity locally so production `createAgentIdentity` can sign
   and publish as that operator;
5. the backend and product contracts let the production-profile sidecar open
   useful account-bound `core` sessions through the HTTP/SQLite/Keychain
   provider wiring;
6. product/backend decide whether successful server-confirmed pairing should
   automatically mint, rotate, or revoke scoped local grants;
7. the parent `puffo-ai/puffoagent` branch is reviewed and merged;
8. npm credentials with access to the `@puffo-ai` org are available so
   `@puffo-ai/agent-core` can be published after release approval.

For the fastest MVP, Web can keep signing/registering the agent association
with its existing browser identity and pass `coreIdentity.source = "web_signed"`
to localhost. That unblocks the local create-time association path, but it is
not the final native security model and still needs a native session/key
bootstrap story before production message transport is complete. The concrete
Web-local handoff and old Web keystore references are tracked in
[`AGENT_CORE_WEB_SIGNED_MVP.md`](AGENT_CORE_WEB_SIGNED_MVP.md).

The concise owner-by-owner unblock checklist is tracked in
[`AGENT_CORE_PRODUCTION_UNBLOCK_CHECKLIST.md`](AGENT_CORE_PRODUCTION_UNBLOCK_CHECKLIST.md).
