# Agent Core Production Unblock Checklist

This is the short handoff for moving the implemented `agent-core` local MVP
from verified localhost/dev behavior to a reproducible production message loop.

The local package already covers daemon startup, localhost API, Claude/Codex
provider control, macOS policy/sandbox inputs, Rust native core wiring, package
smoke tests, and production-profile sidecar construction. Production remains
blocked by the items below.

## 1. Land The Core Patch

Owner: Rust `core` maintainers.

Current review PR:

```text
https://github.com/puffo-ai/core/pull/18
branch: feature/agent-core-native-bridge
commit: ece389a12da5ce3745a213d54a0d55b1b56e3729
status: open, non-draft, clean as of 2026-05-09
```

Required action:

```bash
git submodule update --init agent-core/core
(cd agent-core/core && git apply ../../docs/patches/agent-core-core-upstream.patch)
```

Patch fingerprint:

```text
size: 106,416 bytes
sha256: 097264a775d1b3884e6fccd8103cc8c45e316399b65785a142a920be0f36ba17
```

What it adds:

- operator-bound agent identity creation in the Rust client SDK;
- Rust-side agent identity publication against `POST /agents` plus
  `POST /certs/slug_binding`, including backend canonical slug rebind;
- blocking HTTP transport for the production server provider;
- current-backend space read route mapping from `GET /spaces` and
  `GET /spaces/{space_id}/events?since=...` into core space event logs;
- current-backend invitation discovery mapping from
  `GET /invites?direction=received` rows with flattened proof material into
  trusted `InvitationDiscoveryItem` values;
- default/dev-tools test gating so default builds avoid dev-only scenarios.

Acceptance gate before moving the parent submodule pointer from the PR branch
commit to the merged upstream revision:

```bash
(cd agent-core/core && cargo build -p puffo-client)
(cd agent-core/core && cargo test -p puffo-client)
(cd agent-core/core && cargo test -p puffo-client --features dev-tools)
```

After PR #18 lands upstream, update `agent-core/core` to the merged committed
revision and rerun:

```bash
(cd agent-core && npm test)
(cd agent-core && npm run test:native)
(cd agent-core && npm run check:package)
(cd agent-core && npm run smoke:package)
(cd agent-core && AGENT_CORE_NATIVE_BUILD_PROFILE=prod npm run smoke:package)
(cd agent-core && npm run build:native:dev)
```

## 2. Settle Backend Contracts

Owner: backend/product.

Required contract decisions:

- pairing routes or local bridge flow that bind this daemon to the logged-in
  Web user. Backend PR https://github.com/puffo-ai/puffo-server/pull/26 now
  implements the proposed endpoint shapes from
  [`AGENT_CORE_PAIRING_CONTRACT.md`](AGENT_CORE_PAIRING_CONTRACT.md) against
  `dev`, including one-time daemon `authToken` handoff, but it still needs
  merge/deploy and product verification. The local daemon now has
  `ServerPairingGateway` wired to those start/poll routes and forwards the
  one-time token into native `confirmPairing` without echoing it;
- exact server/local pairing semantics. The native sidecar now signs
  production HTTP requests with Rust-side `x-puffo-*` headers instead of the
  earlier provisional bearer transport, but the product still needs to decide
  what the pairing token grants locally;
- agent identity registration is implemented locally against the current
  server-side `POST /agents` plus `POST /certs/slug_binding` contract. Rust
  submits the full cert/attestation bundle, re-signs the `SlugBinding` when the
  backend allocates a canonical slug, and exposes only high-level status fields
  to Node. It still needs end-to-end verification after pairing provisions the
  operator identity locally. For MVP, Web can continue signing/registering the
  association from its browser-held identity and pass only `coreIdentity`
  metadata to the daemon, but that is a Web security-model tradeoff and still
  needs native session/key bootstrap for production message transport;
- message transport is partially implemented server-side through
  `POST /messages`, `GET /messages/pending`, and `POST /messages/ack`, and the
  native sidecar now injects signed auth headers from Rust. This still needs
  end-to-end verification after pairing is wired;
- space and invite transport are aligned between the local core PR and backend
  PR https://github.com/puffo-ai/puffo-server/pull/25. That backend PR adds the
  signer device/subkey id persistence needed for verifiable space event replay
  and adds flattened invite proof fields so `GET /invites?direction=received`
  can be parsed into trusted `InvitationDiscoveryItem` values. It still must be
  merged to the `dev` branch, deployed from there, and verified end to end
  before production health can drop the `space_invite_sync_contract` blocker;
- realtime notification semantics for production sidecar use. Server has
  WebSocket/pubsub fanout for message and agent-status updates, but the native
  sidecar still needs a settled polling or WS integration path;
- whether server-confirmed pairing should request local Web grants by default.
  The daemon implements the current PR #26 `localWebGrant.mode =
  "daemon_mints"` hint by minting a short-lived management grant on the
  one-time auth-token poll.

Current PR state rechecked on 2026-05-09:

```text
backend PR #25: open, non-draft, clean, CI green
  branch: feature/space-event-signer-ids -> dev
  head: 7e971f775f225ebb41b99bf9c540084c6be2aca3
  local focused tests passed on 2026-05-09:
    `cargo test -p puffo-server invites -- --nocapture`      (6 tests, 249 filtered)
    `cargo test -p puffo-server membership -- --nocapture`   (31 tests, 224 filtered)
    `cargo test -p puffo-server spaces -- --nocapture`       (23 tests, 232 filtered)
backend PR #26: open, non-draft, clean, no reported checks
  branch: feature/agent-core-pairing-contract -> dev
  head: eb1ee2b95d6ea6c1e89af7418580e34f3cccf478
  local focused test: `cargo test -p puffo-server agent_core_pairings -- --nocapture`
    passed on 2026-05-09 with 2 tests, 255 filtered
```

Current production API base URL:

```text
https://api.puffo.ai
```

Current local observation: unauthenticated `curl https://api.puffo.ai` returns
HTTP `401` on this development machine, so the host is reachable here, but the
agent-core auth/message contract is not proven by that check.

Detailed draft: [`AGENT_CORE_SERVER_CONTRACT_NEEDED.md`](AGENT_CORE_SERVER_CONTRACT_NEEDED.md).

## 3. Verify Rust-Side Identity Publication End To End

Owner: agent-core native bridge plus backend/product pairing owners.

Reason: the Rust-side publication path is implemented locally, but it can only
complete in production after pairing restores or provisions the operator
identity in the local encrypted store.

MVP exception: the Web-signed path can create the association certificate outside
native Rust and attach `coreIdentity.source = "web_signed"` to the local daemon.
That lets Web move faster without giving Node key material, but it does not
replace the long-term native signing path or solve production native session
opening by itself. The concrete Web-local handoff is tracked in
[`AGENT_CORE_WEB_SIGNED_MVP.md`](AGENT_CORE_WEB_SIGNED_MVP.md).

Implemented local path:

```text
NativeSession::create_and_publish_agent_identity(agentSlug)
sidecar createAgentIdentity
```

Expected behavior:

- Rust creates the local agent identity material and operator attestation;
- Rust calls `POST /agents` as the operator with signed `x-puffo-*` auth;
- Rust re-signs the `SlugBinding` if the backend returns a canonical slug;
- Rust calls `POST /certs/slug_binding` with the pending token;
- Node receives only high-level status, canonical slug, and the public
  `declaredOperatorPublicKey` anchor read back from the local Rust store.

## 4. Wire Account-Bound Production Sessions

Owner: agent-core native bridge plus backend integration.

Current state:

- production-profile sidecar constructs `NativeCore::for_prod`;
- HTTP + SQLite/SQLCipher + macOS Keychain + signed `x-puffo-*` auth header
  injection are wired;
- `confirmPairing` can activate a server-issued token locally and persist it in
  Keychain in production `apple-keychain` builds, but route auth no longer uses
  it as a bearer token;
- `openAgentSession` routes through the production Rust core construction path.

Still needed:

- real paired account/device state from backend;
- paired operator identity available in the local encrypted store;
- production verification that published agent identities can immediately open
  useful sessions after backend commit;
- backend PR https://github.com/puffo-ai/puffo-server/pull/26 merged and
  deployed for server-confirmed local daemon pairing;
- backend PR https://github.com/puffo-ai/puffo-server/pull/25 merged and
  deployed for signer-id replay plus invitation proof material;
- useful account-bound message sync/send routes.

Production health should stop reporting these blockers only after the above is
true:

```text
backend_pairing_contract
space_invite_sync_contract
```

## 5. Release Gate

Owner: release/package owner.

Do not publish from a dev-profile native sidecar. The package already enforces:

```bash
AGENT_CORE_NATIVE_BUILD_PROFILE=prod npm publish --dry-run
```

The `prepublishOnly` gate rejects non-prod native profiles and runs the pack
manifest verifier. The package `publishConfig.registry` pins release publishes
to `https://registry.npmjs.org/`, so developer-local npm registry mirrors do
not redirect a release.

The unscoped npm name is not usable:
`npm view agent-core --registry=https://registry.npmjs.org/` returns
`agent-core@0.0.1-security`. The package has therefore moved to the scoped
release name `@puffo-ai/agent-core`, while keeping the CLI binary as `agent`.
Registry availability for `@puffo-ai/agent-core` was checked on 2026-05-07;
publishing still requires access to the `@puffo-ai` npm org, but the package
name is no longer blocked by the occupied unscoped name. Web-hosted bootstrap
can still test an alternate package source with the explicit `--package`
argument. `publishConfig.access = public` is set so the scoped package does not
accidentally publish as restricted.

```bash
curl -fsSL https://example.test/agent-core/bootstrap-macos.sh | bash
```

A production release candidate should pass:

```bash
(cd agent-core && npm test)
(cd agent-core && npm run test:native)
(cd agent-core && npm run check:core-patch)
(cd agent-core && npm run check:package)
(cd agent-core && npm run smoke:package)
(cd agent-core && AGENT_CORE_NATIVE_BUILD_PROFILE=prod npm run smoke:package)
(cd agent-core && AGENT_CORE_NATIVE_BUILD_PROFILE=prod npm publish --dry-run)
(cd agent-core && npm run build:native:dev)
```
