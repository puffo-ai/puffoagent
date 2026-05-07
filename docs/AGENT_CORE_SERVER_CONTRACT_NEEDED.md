# Agent Core Server Contract Needed

This is the remaining contract needed to turn the local `agent-core` dev loop
into a production message loop.

Local implementation already owns:

- daemon startup and localhost API;
- local control token and hashed scoped local grants;
- Claude/Codex provider lifecycle;
- macOS sandbox policy inputs, including network deny, denied tools, and extra
  file resource grants;
- provider credential projection into virtual `HOME`;
- explicit provider config/skills projection into virtual `HOME`;
- native Rust sidecar process supervision;
- Rust `core` agent identity creation;
- local encrypted-message dev loop through the sidecar.

Current `core` submodule status: the client crate has HTTP route mappings,
SQLite store support, and a blocking HTTP transport. The local native crate now
has feature-gated production wiring that constructs `ClientSdk::from_providers`
with HTTP transport, SQLite/SQLCipher persistence, macOS Keychain crypto, and a
Rust-side signed `x-puffo-*` HTTP transport. The sidecar also has an explicit
`AGENT_CORE_NATIVE_PROFILE=prod` boundary; Node supplies the default production
server URL and local database path unless env overrides them. The provisional
`confirmPairing` native path can still accept a server-confirmed local pairing
token and persist it to macOS Keychain in production `apple-keychain` builds,
but that token is no longer used as route bearer auth.
The SQLCipher database DEK is generated and loaded inside the Rust sidecar and
stored as a local macOS Keychain generic password, so Node does not have to
forward raw DB key material. The sidecar process also receives a minimal
environment rather than the daemon's full environment, so provider API keys are
not forwarded. It still needs the backend contract to pair the device,
register/publish agent identities, and make account-bound message sessions
useful. Dev sidecars default to `dev_mock`; non-dev sidecars default to `prod`.

Current local production-profile inputs:

```text
AGENT_CORE_NATIVE_PROFILE=prod
AGENT_CORE_AUTH_TOKEN
AGENT_CORE_DATABASE_PATH     # optional override; defaults under AGENT_CORE_HOME
AGENT_CORE_SERVER_URL        # optional override; defaults to https://api.puffo.ai
```

Production backend note: the current Puffo production API base URL is
`https://api.puffo.ai`. That host is IP-allowlisted, so local production-profile
tests can use it directly only from approved networks or machines. The daemon
already defaults provider/environment detection to this URL, and local tests can
override it with `AGENT_CORE_SERVER_URL`. On the current development machine,
an unauthenticated `curl https://api.puffo.ai` returns HTTP `401`, which confirms
the host is reachable here but does not prove any agent-core auth or message
contract is implemented.

The missing production pieces are below.

One local packaging detail remains separate from the server contract: the MVP
still stages the packaged sidecar with `dev-tools` by default, while
`npm run build:native:prod` and `AGENT_CORE_NATIVE_BUILD_PROFILE=prod npm pack`
build and stage a non-dev production-profile sidecar with the macOS
`apple-keychain` provider profile enabled.

## Current Backend Snapshot

Pulled on 2026-05-06 from:

```text
/Users/glimmer/Desktop/projects/puffo.ai/puffo-server
main @ bc0b37d
```

The backend has moved since this draft was first written. `puffo-server/main`
now includes `feat(server): agent integration - schema, read + write endpoints,
ws_pubsub fanout (#15)`.

Implemented backend pieces observed:

- `POST /agents` accepts `{ username, identity_cert, device_cert,
  attestation }` under `SubkeyAuth`, validates the operator/agent key
  relationship, writes `pending_agents`, and returns `{ agent_slug,
  agent_device_id, pending_token, pending_expires_at }`.
- `POST /certs/slug_binding` now also consumes `pending_agents` and persists
  `identity_cert`, `device_cert`, `operator_attestation`, and `slug_binding`
  into `cert_registry`.
- `POST /agents/{agent_slug}/operator/revoke` records operator revocations.
- Message transport exists as `POST /messages`, `GET /messages/pending`, and
  `POST /messages/ack`.
- Space event write transport exists as `POST /spaces/events` with body
  `{ space_id, events }`.
- Agent status/processing telemetry exists as `GET /agents/{agent_slug}/status`,
  `POST /agents/status:batch`, `POST /agents/me/heartbeat`,
  `POST /messages/{message_id}/processing/start`, and
  `POST /messages/{message_id}/processing/end`.

Important mismatches with the earlier `agent-core` production draft:

- Backend auth for these routes is not a server-issued bearer `authToken`.
  Protected routes use signed `x-puffo-*` headers (`x-puffo-slug`,
  `x-puffo-signer-id`, `x-puffo-timestamp`, `x-puffo-nonce`,
  `x-puffo-signature`) verified by `auth_middleware`.
- `GET /messages/pending` derives the target device from the signed device or
  subkey auth context. It does not accept the earlier `slug`, `device_id`,
  `cursor`, or `limit` query contract.
- The pending response shape is `{ "messages": [{ "seq": number,
  "envelope": MessageEnvelope }] }`, not `{ "envelopes": [...],
  "next_cursor": ... }`.
- Ack is `POST /messages/ack` with `{ "envelope_ids": [...] }`, not
  `POST /messages/{envelope_id}/ack`.
- `POST /messages` returns JSON `{ ok, envelope_id, devices_queued }` with
  `201 Created`, not an empty `204`.
- `POST /spaces/events` now expects `{ space_id, events }`; current
  `agent-core` HTTP provider has been updated to send this write shape.
- Space event reads are now route-mapped in the core HTTP provider. It reads
  `GET /spaces` plus `GET /spaces/{space_id}/events?since=&limit=` and maps the
  response into `SpacesSyncResponse`. Backend PR
  https://github.com/puffo-ai/puffo-server/pull/25 adds persistence and response
  fields for `signer_device_id` and `signer_subkey_id`; without that PR merged,
  production clients cannot verify replayed space events end to end.
- Invitation listing is now `GET /invites?direction=sent|received`, not the
  earlier `/me/invitations?slug=...` draft.
- Server-side local daemon pairing now has a draft implementation in backend
  PR https://github.com/puffo-ai/puffo-server/pull/26. That PR adds
  `POST /agent-core/pairings`, `GET /agent-core/pairings/{pairing_id}`, and
  `POST /agent-core/pairings/{pairing_id}/confirm` against `dev`, but it still
  needs merge/deploy and product verification.

Implication for `agent-core`: the production blocker should be narrowed. Agent
identity registration, basic message transport, and current-backend space/invite
route mapping now exist locally. The local native sidecar now has Rust-side
signed `x-puffo-*` request injection and Rust-side agent identity publication
for production HTTP routes. The local daemon now consumes the PR #26 pairing
start/poll routes through `ServerPairingGateway`, but production is not
end-to-end unblocked yet: pairing remains pending on backend PR #26
merge/deploy and product/native bootstrap verification, and puffo-server PR #25
must merge to `dev` and deploy from that branch before space/invite sync can be
trusted in production.

Follow-up backend PR observed on 2026-05-06:

```text
https://github.com/puffo-ai/puffo-server/pull/25
branch: feature/space-event-signer-ids
base: dev
commit: 7e971f775f225ebb41b99bf9c540084c6be2aca3
status: open, non-draft, mergeable as of 2026-05-07
```

That PR adds `signer_device_id` and `signer_subkey_id` persistence/response
fields for replayed space events. It also keeps the existing invite metadata
fields while adding flattened `projection`, `invitation_event`, and `bootstrap`
proof fields to received invite rows. Core PR #18 parses that shape into
trusted `InvitationDiscoveryItem` values and ignores metadata-only rows. The PR
now targets the `dev` branch, which was created from `main @ bc0b37d`, so this
server-side contract can be reviewed and integrated without immediately
targeting production `main`.

Pairing contract backend PR observed on 2026-05-06:

```text
https://github.com/puffo-ai/puffo-server/pull/26
branch: feature/agent-core-pairing-contract
base: dev
commit: 50982d550c761530b9ea02ba4ae4c9e448f998c6
status: open, non-draft, mergeable as of 2026-05-07
```

That PR adds the daemon-started pairing table and routes, authenticated Web
confirmation, one-time raw `authToken` handoff to the polling daemon, and tests
for pairing flow plus migrations on a fresh database. It does not restore or
provision local operator identity material by itself.

## MVP HTTP Contract Draft

This section is the concrete minimum contract the backend and product flow need
to settle before `agent-core` can run useful production sessions. It is based
on the current `core` client `HttpServerProvider` plus the `agent-native`
production wrapper.

Earlier transport assumptions, now known to be stale for `puffo-server/main`
agent/message routes:

- every request from the production native sidecar includes
  `Authorization: Bearer <server-issued-agent-core-token>`;
- JSON requests use `Content-Type: application/json`;
- routes that return no body should return any 2xx status, preferably `204`;
- response field names should match the Rust/serde snake_case names shown
  below;
- server errors should be 4xx/5xx with JSON when possible, but the Rust client
  currently only depends on the HTTP status for rejection classification.

Server-issued local sidecar token:

```json
{
  "authToken": "opaque short or refreshable token",
  "operatorSlug": "alice",
  "accountId": "acct_...",
  "expiresAt": "2026-05-06T19:00:00.000Z"
}
```

The native sidecar only needs `authToken` today. `operatorSlug`, `accountId`,
and `expiresAt` are product/backend metadata the local daemon can use for UI,
logging, and future refresh policy once the contract exists.

Core HTTP routes already encoded in the Rust client:

| Purpose | Request | Response |
| --- | --- | --- |
| Sync certs | `GET /certs/sync?cursor=&slugs=&subkey_ids=&device_ids=` | `{ "cache": CertificateCache, "next_cursor": "..." or null }` |
| Post signed space/channel events | current backend: `POST /spaces/events` body `{ "space_id": "...", "events": [SignedEvent] }` | 2xx JSON/empty accepted by provider |
| Sync space events | current backend: `GET /spaces`, then `GET /spaces/{space_id}/events?since=&limit=` | mapped locally to `{ "spaces": SpaceEventLog[], "invitations": InvitationDiscoveryItem[], "next_cursor": "..." or null, "has_more": false }`; replay signer ids depend on PR #25 |
| List invitations | current backend PR #25: `GET /invites?direction=received` | rows with `projection`, `invitation_event`, and `bootstrap` are parsed as trusted `InvitationDiscoveryItem` values; metadata-only rows are ignored |
| Send encrypted message | current backend: `POST /messages` body `MessageEnvelope` | `201` JSON `{ "ok": true, "envelope_id": "...", "devices_queued": number }` accepted by provider |
| Fetch pending messages | current backend: `GET /messages/pending` under signed device/subkey auth | `{ "messages": [{ "seq": number, "envelope": MessageEnvelope }] }` mapped locally to envelopes |
| Ack message | current backend: `POST /messages/ack` body `{ "envelope_ids": ["..."] }` | 2xx JSON/empty accepted by provider |
| Realtime connect | `POST /v1/subscribe/connect` | `204` |
| Realtime poll | `GET /v1/subscribe/poll?consumer_id=&cursor=&limit=` | `{ "events": RealtimeEvent[], "next_cursor": 0 }` |

`CertificateCache` minimum shape:

```json
{
  "identities": { "alice": { "...": "IdentityCert" } },
  "slug_bindings": { "alice": { "...": "SlugBinding" } },
  "devices": { "device_id": { "...": "DeviceCert" } },
  "subkeys": { "subkey_id": { "...": "SubkeyCert" } },
  "device_revocations": { "device_id": { "...": "DeviceRevocation" } }
}
```

`MessageEnvelope` minimum shape:

```json
{
  "type": "message_envelope",
  "version": 1,
  "envelope_id": "env_...",
  "envelope_kind": "channel",
  "sender_slug": "alice-agent",
  "sent_at": 1778094000000,
  "space_id": "sp_...",
  "channel_id": "ch_...",
  "recipient_slug": null,
  "content_nonce": "...",
  "content_ciphertext": "...",
  "recipients": [
    {
      "device_id": "device_...",
      "hpke_enc": "...",
      "wrapped_content_key": "..."
    }
  ]
}
```

For direct messages, `envelope_kind` is `"dm"`, `recipient_slug` is set, and
`space_id`/`channel_id` are normally null.

Agent identity publication is now implemented on the local Rust side against
the pulled backend contract. Node still receives only high-level status fields
(`operatorSlug`, canonical `agentSlug`, identity type, and optional metadata);
the signed cert/attestation material stays in Rust.

The current production flow is:

```text
POST /agents
POST /certs/slug_binding
```

`POST /agents` is signed as the operator and sends:

```json
{
  "username": "alice-agent",
  "identity_cert": "...",
  "device_cert": "...",
  "attestation": "..."
}
```

The backend allocates the canonical `agent_slug` by applying its username slug
policy. When that differs from the requested local slug, Rust re-signs the
agent `SlugBinding` for the canonical slug before calling
`POST /certs/slug_binding` with:

```json
{
  "pending_token": "pending_...",
  "slug_binding": "..."
}
```

This keeps Node out of the publication bundle while matching the current
`puffo-server` two-phase pending-agent contract.

Pairing is not encoded in the Rust client HTTP provider yet; it is the product
handoff that produces the sidecar token above. The concrete pairing handoff is
tracked in [`AGENT_CORE_PAIRING_CONTRACT.md`](AGENT_CORE_PAIRING_CONTRACT.md).
Backend PR #26 implements this MVP shape against `dev`, and the local daemon
now calls the start/poll side of it:

```text
POST /agent-core/pairings
GET  /agent-core/pairings/{pairingId}
POST /agent-core/pairings/{pairingId}/confirm  # authenticated Web user
```

The daemon calls the first two with a local pairing public nonce, while Web
confirms the pairing under the logged-in user. After confirmation, the server
returns the sidecar `authToken` once to the daemon, not to arbitrary browser
origins. The local `ServerPairingGateway` passes it to native `confirmPairing`
for Rust-side activation and Keychain persistence, and the localhost response
does not echo the token.

## 1. Device Pairing

Local API already exposes:

```text
POST /pairing/start
GET  /pairing/:pairingId
POST /pairing/confirm
```

`POST /pairing/start` and `GET /pairing/:pairingId` are public loopback routes
so Web can connect a fresh daemon before it has a local management grant.
Browser callers must come from a trusted Origin before a confirmed poll can
return a local grant. Direct `POST /pairing/confirm` remains token-protected
because it accepts the server-issued native auth token.

Needed server behavior:

- create a short-lived pairing request for this local daemon/device;
- return a pairing id and user-facing confirmation code or URL;
- let the Web app confirm that pairing under the logged-in account;
- return an operator slug and any server-issued device/account binding material
  needed by Rust `core`;
- return or exchange a server-issued local sidecar auth token that the daemon
  can hand to native `confirmPairing` for Rust-side activation and Keychain
  persistence.

PR #26 covers the route shape, authenticated confirm, and one-time token
handoff. The local daemon consumes that handoff and can mint a short-lived local
Web grant when the server returns `localWebGrant.mode = "daemon_mints"`.
Remaining product/native work is deciding whether the confirmed pairing should
also restore/enroll local operator identity material, or whether the MVP keeps
Web signing and treats pairing as local daemon authorization only.

The local daemon should not trust a Web origin by itself. Server confirmation
must bind the pairing request to the authenticated Web user.

## 2. Agent Identity Registration

Rust `core` now creates an operator-bound agent identity locally:

```text
operator session -> create_agent_identity(agentSlug)
```

Needed server behavior:

- accept the agent identity certificate and operator attestation generated by
  `core`;
- verify that the operator is allowed to register that agent;
- persist the agent slug as an addressable participant;
- expose the resulting identity/cert material through normal cert sync.

Without this, the local agent can create a valid local identity but other
clients cannot address or verify it in production.

MVP Web-signed alternative: Web can continue using the browser IndexedDB
operator identity to sign/register the agent association and then call local
`POST /agents` with `coreIdentity.source = "web_signed"`. In that path the
local daemon does not see operator private keys and does not call native
`createAgentIdentity`, but production message transport still needs Rust core
to have or restore whatever local agent/session material is required to open
that `coreIdentity`.

## 3. Message Transport

The native sidecar currently has these Node-facing operations:

```text
openAgentSession
syncOnce
processPendingMessages
sendChannelReply
sendDirectReply
closeSession
```

Needed server behavior:

- cert sync for operator and agent identities;
- space/channel event sync;
- pending encrypted message retrieval for the agent device;
- encrypted channel/direct message send;
- message ack or cursor advancement semantics;
- realtime notification or polling cursor semantics.

The Rust client crate already has HTTP server provider path mappings and a
blocking HTTP transport. The remaining decision is the concrete backend contract
and auth headers used by the local sidecar.

## 4. Local-Web Authorization

The daemon currently protects management routes with a local control token and
can mint hashed, expiring, revocable management grants from that control token.
Discovery remains public:

```text
GET /health
GET /providers
POST /local-grants          # control token only
DELETE /local-grants/:id    # control token only
POST /local-control-token/rotate  # control token only
```

Browser access is permissive by default for MVP discovery, but production-like
runs can set `AGENT_CORE_ALLOWED_ORIGINS` to restrict CORS to exact HTTP(S)
origins. That only controls browser callers; local authorization still depends
on the control token or a scoped local grant.
Local control-token rotation is implemented locally and clears existing local
grants; server-confirmed pairing still needs a product decision for when Web
should receive, rotate, or revoke scoped grants automatically.

Needed product decision:

- whether Web asks the user to paste the local control token;
- or whether server-confirmed pairing authorizes the daemon to mint a scoped
  local grant and return it to the Web app;
- the user consent step and grant rotation/revocation policy after pairing.

Until this is decided, production Web can use the manual local token step or a
token-minted short-lived local grant, but cannot complete a server-confirmed
pairing flow that automatically hands the Web app a local management grant.
