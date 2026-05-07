# Core Upstream Handoff For Agent Core

This handoff records the `agent-core/core` submodule changes required by the
local `agent-core` native bridge. These changes have been committed to an
upstream review branch:

```text
https://github.com/puffo-ai/core/pull/18
branch: feature/agent-core-native-bridge
commit: ece389a12da5ce3745a213d54a0d55b1b56e3729
status: open, mergeable as of 2026-05-06
```

The parent `agent-core/core` gitlink currently records this PR commit so fresh
clones can reproduce the verified build while the review branch remains
reachable. The PR still must land in the upstream `puffo-ai/core` repository
before release so the submodule pointer can move to a stable merged revision.

The full submodule diff from the recorded upstream patch base to the PR commit
is exported as:

```text
docs/patches/agent-core-core-upstream.patch
docs/patches/agent-core-core-upstream.base
```

Current exported patch fingerprint:

```text
size: 106,416 bytes
sha256: 097264a775d1b3884e6fccd8103cc8c45e316399b65785a142a920be0f36ba17
```

Regenerate it from the current `agent-core/core` diff against the recorded
patch base with:

```bash
(cd agent-core && npm run export:core-patch)
```

That export command rewrites the patch file and immediately runs the
bidirectional patch verifier. Both patch commands require the `agent-core/core`
submodule to exist; on a fresh checkout, initialize it first with:

```bash
git submodule update --init agent-core/core
```

Apply it from the parent repo with:

```bash
(cd agent-core/core && git apply ../../docs/patches/agent-core-core-upstream.patch)
```

The patch can be checked from the `agent-core/` package with:

```bash
npm run check:core-patch
```

That command verifies the exported patch against the commit recorded in
`docs/patches/agent-core-core-upstream.base`. It checks that the current
submodule diff from that base exactly matches the patch when the submodule is
checked out on the PR branch, checks reverse-apply in that state, and applies
the patch to a temporary clean detached worktree at the recorded base. The
underlying manual command for the forward-apply half is:

```bash
tmp="$(mktemp -d /tmp/agent-core-core-patch-check-XXXXXX)"
(cd agent-core/core && git worktree add --detach "$tmp" c623bd6b336354bf0425c0473d6e4da63e801164 >/dev/null)
git -C "$tmp" apply --check /Users/glimmer/Desktop/projects/puffo.ai/agent/docs/patches/agent-core-core-upstream.patch
(cd agent-core/core && git worktree remove "$tmp")
```

Recorded upstream patch base:

```text
c623bd6b336354bf0425c0473d6e4da63e801164 agent-core/core
```

Current parent submodule pointer:

```text
ece389a12da5ce3745a213d54a0d55b1b56e3729 agent-core/core
```

PR #18 is intentionally based on `c623bd6...` even though upstream `main` has
advanced. GitHub currently reports the PR as mergeable. Do not rebase the
review branch just to chase `main`: doing so would make the exported patch from
the recorded base include unrelated upstream commits. After PR #18 merges,
update the parent `agent-core/core` pointer directly to the merged upstream
revision.

Current upstream PR scope:

```text
32 files changed, 1,963 insertions(+), 117 deletions(-)
```

## Patch Groups

### 1. Operator-Bound Agent Identity

Purpose: let the agent runtime ask the Rust client SDK to create a local agent
identity under an operator identity, without Node handling key material.

Primary files:

```text
crates/client/src/api/command.rs
crates/client/src/api/session.rs
crates/client/src/ports/crypto.rs
crates/client/src/providers/crypto/core_crypto.rs
crates/client/src/providers/crypto/unavailable.rs
crates/client/tests/contract_client_api.rs
specs/001-client-sdk/contracts/client-api.md
```

Expected API shape:

```rust
CreateAgentIdentityCommand { slug }
ClientSession::create_agent_identity(command) -> Result<ClientSession>
CryptoPort::create_agent_identity(slug, operator_slug, issued_at_ms)
```

The new identity is saved through the existing client store provider and then
activated through the existing crypto provider. The agent-native bridge depends
on this to create `coreIdentity` records for local agents.

### 2. Agent Identity Publication

Purpose: publish an operator-bound local agent identity to the current backend
without passing identity cert material or slug-binding signing through Node.

Primary files:

```text
crates/client/src/api/session.rs
crates/client/src/domain/identity/model.rs
crates/client/src/ports/server/account.rs
crates/client/src/providers/crypto/core_crypto.rs
crates/client/src/providers/server/http/provider.rs
crates/client/src/providers/server/mock/provider.rs
crates/client/src/providers/server/runtime.rs
crates/client/tests/contract_client_api.rs
crates/client/tests/contract_server_ports.rs
```

Key behavior:

```text
ClientSession::create_and_publish_agent_identity(command)
POST /agents sends identity_cert, device_cert, and operator attestation
POST /certs/slug_binding commits the pending agent slug binding
backend-allocated canonical slugs are rebound and re-signed in Rust
agent identities persist operator_attestation for later activation
```

Production end-to-end success still depends on pairing provisioning or restoring
the operator identity locally before the sidecar calls this method.

### 3. Blocking HTTP Transport

Purpose: give production-profile native sidecars a real HTTP server provider
implementation instead of the unavailable transport placeholder.

Primary files:

```text
crates/client/Cargo.toml
crates/client/src/providers/mod.rs
crates/client/src/providers/server/http/mod.rs
crates/client/src/providers/server/http/provider.rs
crates/client/src/providers/server/http/transport.rs
crates/client/tests/contract_server_ports.rs
Cargo.lock
```

Key behavior:

```text
HttpServerProvider::new(api_url) uses BlockingHttpTransport::default()
BlockingHttpTransport sends GET/POST/PATCH/PUT/DELETE with ureq
HTTP JSON responses decode into HttpBody::Json
204 responses map to empty responses
non-2xx responses still return HttpResponse for provider-level decoding
transport errors map to SdkError::NetworkUnavailable
request paths must be relative to api_url
query/path segments are percent-encoded
POST /spaces/events sends the current backend write shape: { space_id, events }
HandleBackedCryptoProvider can sign HTTP requests as the active subkey signer
GET /spaces plus GET /spaces/{space_id}/events?since=... maps current backend reads
GET /invites?direction=received parses backend rows with flattened invitation proof material into InvitationDiscoveryItem values
metadata-only invite rows are still ignored rather than trusted as invitations
```

Production route/auth semantics are still gated by pairing. This patch provides
the client-side transport and signing helper needed by `agent-native`.

### 4. Default Profile / Test Gating

Purpose: keep default client builds from compiling dev-only integration paths,
while still allowing the `dev-tools` scenario suite.

Primary files:

```text
crates/client/tests/contract_profiles.rs
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
```

Expected result:

```text
cargo test -p puffo-client
cargo test -p puffo-client --features dev-tools
```

Both pass. The default run should not require dev-only mock/scenario helpers.

## Required Verification Before Updating The Submodule Pointer

Run from `agent-core/core` after applying the patch upstream:

```bash
cargo build -p puffo-client
cargo test -p puffo-client
cargo test -p puffo-client --features dev-tools
```

Then run from `agent-core` after updating `agent-core/core` to the committed
upstream revision:

```bash
npm test
npm run check:core-patch
npm run test:native
npm run smoke:package
AGENT_CORE_NATIVE_BUILD_PROFILE=prod npm run smoke:package
AGENT_CORE_NATIVE_BUILD_PROFILE=prod npm publish --dry-run
npm run build:native:dev
```

`npm run build:native:dev` at the end restores the local staged native sidecar to
the default dev profile after prod package checks.

## Remaining Non-Upstream Blockers

Landing this upstream patch removes the reproducibility blocker, but it does
not complete production message transport by itself. Backend/product contracts
are still needed for:

```text
device pairing
local operator identity provisioning after pairing
production agent identity publication verification
account-bound message sync/send verification
backend PR #25 merge/deploy from `dev` for persisted space event signer ids
backend PR #25 merge/deploy from `dev` for invitation discovery proof material
final auth header/cursor/ack semantics
server-confirmed local Web grant handoff policy
```
