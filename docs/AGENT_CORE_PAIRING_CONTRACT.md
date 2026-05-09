# Agent Core Pairing Contract

This is the concrete MVP handoff for the only remaining production contract
that is not implementable entirely inside `agent-core`: binding a local daemon
to the logged-in Web user and provisioning the operator identity needed by the
Rust core.

The local daemon already exposes:

```text
POST /pairing/start
GET /pairing/:pairingId
POST /pairing/confirm
```

Those routes are local-only daemon routes. In the normal daemon process,
`POST /pairing/start` and `GET /pairing/:pairingId` are public on loopback and
go through `ServerPairingGateway`, which calls the backend PR #26 start/poll
contract. This lets Web pair a fresh daemon before it has a local management
grant. Because a confirmed poll can return a local Web grant, browser callers
must come from a trusted Origin (`https://chat.puffo.ai`,
`https://app.puffo.ai`, or local dev ports 3000/5173 by default; replaced by
`AGENT_CORE_ALLOWED_ORIGINS` when configured). Direct `POST /pairing/confirm`
remains token-protected because it accepts the lower-level native-token handoff.
When a confirmed server poll returns the one-time `authToken`, the daemon
forwards it into native `confirmPairing`, where production macOS builds persist
it in Keychain. The local response does not echo the raw token.
The daemon only sends pairing requests to HTTPS backend base URLs, or loopback
HTTP base URLs for local development. The backend pairing responses must use a
JSON-compatible `Content-Type`; redirects and non-JSON media types fail closed.

## Required Outcome

After pairing succeeds:

- the local daemon is bound to the authenticated Puffo account;
- the local Rust store has or can restore the operator identity for that
  account;
- the native sidecar can open the operator session and call
  `create_and_publish_agent_identity`;
- the daemon can create a short-lived local Web management grant, if product
  chooses automatic Web authorization after pairing;
- Node never receives identity private keys, SQLCipher DEKs, or cert signing
  material.

## Backend Routes

These routes are backend routes implemented by backend PR #26 against `dev`.
They still need merge/deploy and product verification before production daemon
pairing can be considered complete.

### Start Pairing

Called by the local daemon.
`localApiOrigin` is derived by the daemon from the actual loopback request host;
it is not accepted from the local Web request body.
If local Web supplies `pairingPublicNonce`, it must be an unpadded base64url
string from 32 to 128 characters; otherwise the daemon generates a 32-byte
nonce. The local `POST /pairing/start` body is strict and accepts only
`pairingPublicNonce`; unknown fields fail before a backend request is made.

```text
POST /agent-core/pairings
```

Request:

```json
{
  "pairingPublicNonce": "base64url-random",
  "daemonVersion": "0.1.0",
  "platform": "darwin",
  "arch": "arm64",
  "localApiOrigin": "http://127.0.0.1:63387"
}
```

Response:

```json
{
  "pairingId": "pair_...",
  "userCode": "ABCD-EFGH",
  "confirmUrl": "https://app.puffo.ai/agents/pair?pairingId=pair_...",
  "expiresAt": "2026-05-06T19:00:00.000Z",
  "pollAfterMs": 1000
}
```

The daemon may display `userCode` or pass `confirmUrl` to Web. The response must
not grant account access yet. `confirmUrl` must be an HTTPS URL without
embedded username or password credentials; loopback HTTP is accepted only for
local development. Unknown top-level fields are rejected.

### Poll Pairing

Called by the local daemon until confirmed, expired, or canceled.

```text
GET /agent-core/pairings/{pairingId}
```

Pending response:

```json
{
  "status": "pending",
  "expiresAt": "2026-05-06T19:00:00.000Z",
  "pollAfterMs": 1000
}
```

Pending, expired, and canceled poll responses must not include
`operatorSlug`, `accountId`, `authToken`, `operatorBootstrap`, or
`localWebGrant`. The daemon treats those as confirmed-only fields and rejects
the response before forwarding anything to native core or Web.

Confirmed response:

```json
{
  "status": "confirmed",
  "operatorSlug": "alice",
  "accountId": "acct_...",
  "authToken": "server-issued-local-sidecar-token",
  "operatorBootstrap": {
    "kind": "restore_or_enroll",
    "operatorSlug": "alice",
    "payload": {}
  },
  "localWebGrant": {
    "mode": "daemon_mints",
    "ttlMs": 900000
  }
}
```

The `authToken` is handed to local `POST /pairing/confirm` and then to the
native sidecar. Production `apple-keychain` builds persist it in Keychain.
Current signed HTTP route auth still uses Rust-side `x-puffo-*` signatures; the
token is pairing/session bootstrap state, not a replacement for request
signatures. Confirmed poll responses are also parsed as a strict top-level
schema; unexpected fields are rejected before the daemon forwards anything to
native core or Web.
The localhost `GET /pairing/:pairingId` response only exposes a Web-safe
`operatorBootstrap` summary with `kind` and `operatorSlug`; any backend
`restore_or_enroll.payload` is not returned to the browser.

Backend PR https://github.com/puffo-ai/puffo-server/pull/26 implements this
draft shape against `dev`: start, poll, and authenticated Web confirm. It stores
only an auth-token hash server-side and returns the raw `authToken` once to the
polling daemon after confirmation. It still needs merge/deploy and product
verification before the local daemon can treat it as production pairing.
The local daemon now has `ServerPairingGateway` wired to the start/poll routes;
when a confirmed poll includes the one-time `authToken`, the daemon hands it to
native `confirmPairing` and does not echo it in the localhost response.

### Confirm Pairing

Called by the authenticated Web app.

```text
POST /agent-core/pairings/{pairingId}/confirm
```

Request:

```json
{
  "userCode": "ABCD-EFGH",
  "allowLocalWebGrant": true
}
```

Response:

```json
{
  "status": "confirmed",
  "operatorSlug": "alice",
  "expiresAt": "2026-05-06T19:00:00.000Z"
}
```

Server confirmation must be bound to the authenticated Web session. The local
daemon must not trust browser origin alone as proof of account ownership.

## Operator Identity Bootstrap

The backend/product decision still needed is the `operatorBootstrap` shape.
Acceptable MVP options are:

```json
{ "kind": "existing_local_identity", "operatorSlug": "alice" }
```

Use when the operator identity already exists in the local encrypted Rust store.

```json
{ "kind": "restore_or_enroll", "operatorSlug": "alice", "payload": {} }
```

Use when the daemon must restore or enroll the operator identity after pairing.
The sensitive restore/enrollment material must be consumed by Rust-side core
code. Node may carry opaque status fields but should not parse identity private
keys or signing material, and the localhost response must not expose
`payload` back to Web.

The daemon rejects unknown `operatorBootstrap.kind` values, mismatched
`operatorSlug` values, and unsupported top-level fields. For
`existing_local_identity`, only `kind` and `operatorSlug` are accepted.

For the fastest Web-signed MVP, Web may keep using its browser-held operator
identity to sign/register the agent association and then call local
`POST /agents` with `coreIdentity.source = "web_signed"`. That removes native
operator signing from the agent-create path, but it does not by itself provision
native agent key material for production message transport. A start still needs
the Rust core session to open that agent identity through local restore/import
or another native session bootstrap.
The exact Web-local handoff is tracked in
[`AGENT_CORE_WEB_SIGNED_MVP.md`](AGENT_CORE_WEB_SIGNED_MVP.md).

If the product flow cannot provision the operator identity locally, production
`createAgentIdentity` remains blocked even if the daemon receives an
`authToken`.

## Local Web Grant Policy

Local management routes already support hashed, expiring, revocable grants:

```text
POST /local-grants          # local control token only
DELETE /local-grants/:id    # local control token only
```

The product decision is whether server-confirmed pairing should automatically
authorize Web access to the local daemon.

Recommended MVP:

- pairing confirmation authorizes the daemon to mint one scoped local
  management grant;
- grant TTL defaults to 15 minutes;
- grant token is returned only once to the Web app;
- local control-token rotation revokes all grants;
- grants cannot mint or revoke other grants.

Fallback MVP:

- Web asks the user to paste the printed local control token;
- no automatic local grant is minted after server pairing.

## Acceptance Tests

Backend/product pairing is ready when these flows can pass:

1. Local daemon starts pairing, Web confirms as a logged-in user, daemon polls a
   confirmed result, and local `/pairing/confirm` activates the returned token
   without echoing it.
2. After pairing, native production health no longer reports
   `backend_pairing_contract`.
3. `createAgentIdentity` opens the paired operator session and successfully
   publishes an agent via `POST /agents` plus `POST /certs/slug_binding`.
4. The agent can open an account-bound production session and run at least one
   message sync/send smoke path.
5. If automatic Web grants are enabled, Web can call local management routes
   with the minted grant, cannot mint other grants, and loses access after TTL,
   revoke, or local control-token rotation.

## Current Blockers

- Core PR https://github.com/puffo-ai/core/pull/18 must merge so fresh clones
  can reproduce the Rust-side production provider and agent identity
  publication path.
- Backend PR https://github.com/puffo-ai/puffo-server/pull/25 must merge and
  deploy from `dev` before production space/invite sync can be trusted end to
  end.
- Backend PR https://github.com/puffo-ai/puffo-server/pull/26 must merge and
  deploy from `dev` before server-confirmed daemon pairing can be used in
  production.
- Product/backend still need to choose whether confirmed pairing restores or
  provisions native operator identity material, or whether MVP creation uses
  the Web-signed shortcut and leaves native session bootstrap for a follow-up.
