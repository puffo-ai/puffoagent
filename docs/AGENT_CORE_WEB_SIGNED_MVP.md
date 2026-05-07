# Agent Core Web-Signed MVP

This note records the fastest MVP path discussed on 2026-05-06 and confirmed
for MVP on 2026-05-07: keep agent association signing in the existing Web
client, then attach the already-signed agent identity metadata to the local
daemon.

## Existing Web Identity Storage

The current Web client already persists the logged-in user's identity locally:

- `puffo-core-han-group/client/web/src/crypto/keystore.ts` defines
  `StoredIdentity` with `root_secret_key`, `device_signing_secret_key`, and
  `kem_secret_key`.
- The same file stores identities in IndexedDB `puffo-keystore` and encrypts
  private-key fields with an IndexedDB-held non-extractable WebCrypto
  AES-GCM wrapping key.
- `puffo-core-han-group/client/web/src/enrollment/signup.ts` calls
  `keyStore.saveIdentity(identity)` after signup.
- `puffo-core-han-group/client/web/src/enrollment/password-auth.ts` calls
  `keyStore.saveIdentity(identity)` after password-auth signup/login restore.

That means Web can sign an operator-to-agent association with its existing
browser-held operator identity, but this keeps the MVP on the browser security
model. XSS or compromised Web JS can still ask the keystore to decrypt and use
the private keys.

## Legacy Web Bridge Callers

The existing Web bridge code is still shaped for the old Python daemon:

- `puffo-core-han-group/client/web/src/bridge/client.ts` probes
  `GET /v1/info`, signs local bridge requests with the browser-held device key,
  and then calls old `/v1/pair`, `/v1/agents`, runtime/profile/files routes.
- `puffo-core-han-group/client/web/src/bridge/provision.ts` already creates
  an agent IdentityCert, DeviceCert, OperatorAttestation, and slug binding in
  the browser, then registers them with `/agents` and `/certs/slug_binding`.
- That same provisioner currently sends `identity_bundle.root_secret_key`,
  `device_signing_secret_key`, and `kem_secret_key` to the old local
  `/v1/agents` endpoint.

For the new `agent-core` daemon, keep the browser-side certificate signing and
server registration pieces, but replace the local handoff. The local daemon
does not implement old `/v1/pair` or secret-bundle `/v1/agents`; `GET
/v1/info` exists only as read-only daemon discovery compatibility.

## Current Local Daemon Contract

The implemented local API accepts only public agent identity metadata:

```json
{
  "name": "Alice Agent",
  "provider": "codex",
  "coreIdentity": {
    "operatorSlug": "alice",
    "agentSlug": "alice-agent",
    "identityType": "agent",
    "declaredOperatorPublicKey": "base64-or-encoded-operator-public-key",
    "source": "web_signed"
  }
}
```

`POST /agents` requires `declaredOperatorPublicKey` so the stored metadata keeps
the public operator anchor declared by the signed agent IdentityCert. That
public anchor is trimmed and capped at 4096 characters at the API, runtime,
state, and native bridge boundaries. The daemon rejects unsupported
`coreIdentity` fields such as `root_secret_key`,
`device_signing_secret_key`, `kem_secret_key`, or arbitrary secret bundles.
It also rejects `coreIdentity.source = "native"` on this local handoff; that
source is reserved for identities actually created by the Rust native path.
Node persists only high-level metadata and skips native `createAgentIdentity`.

This is intentionally narrower than "Web sends all agent secrets to the local
daemon". It lets Web move ahead with certificate registration and local agent
management without putting private keys into the Node daemon by default.

## Web Client Integration Surface

The old Web bridge client is still present for the Python daemon flow, but the
new non-UI `agent-core` integration surface now lives in:

- `puffo-core-han-group/client/web/src/agent-core/client.ts`
- `puffo-core-han-group/client/web/src/agent-core/provision.ts`
- `puffo-core-han-group/client/web/src/agent-core/types.ts`
- `puffo-core-han-group/client/web/src/agent-core/index.ts`

Review branch / PR:
https://github.com/puffo-ai/puffo-core-han-group/pull/52

`AgentCoreHttpClient` uses the new localhost auth model (`Authorization:
Bearer <local-token-or-grant>` plus optional `X-Agent-Core-*` account context)
and does not emit the old signed `x-puffo-*` local bridge headers.
When a server-confirmed pairing poll returns a daemon-minted `localGrant`,
`pollPairingAndUseGrant` installs that grant and the confirmed account context
for subsequent management calls. Non-confirmed pairing responses never replace
the current local authorization, even if an unexpected `localGrant` field is
present.
When Web starts from the printed local control token instead, `createAndUseLocalGrant`
exchanges it for a scoped local grant and then uses that grant for later
management calls.

`provisionAgentCore` keeps the existing browser-side certificate flow:

1. generate the agent root/device/KEM keys in Web memory;
2. create the agent `IdentityCert`, `DeviceCert`, and `OperatorAttestation`;
3. register those certs with the Puffo server and publish the slug binding;
4. call local `POST /agents` with only public `coreIdentity` metadata.

The localhost agent-create body intentionally excludes `identity_bundle`,
`root_secret_key`, `device_signing_secret_key`, and `kem_secret_key`. Focused
Web tests cover this boundary in
`puffo-core-han-group/client/web/tests/agent-core-client.test.ts` and
`puffo-core-han-group/client/web/tests/agent-core-provision.test.ts`.

Minimal Web-side call shape:

```ts
import {
  AgentCoreHttpClient,
  provisionAgentCore,
} from "../agent-core";

const agentCore = new AgentCoreHttpClient("http://127.0.0.1:63387", {
  token: printedLocalControlToken,
  account: { accountId, operatorSlug: operator.slug },
});

await agentCore.createAndUseLocalGrant(15 * 60 * 1000);

await provisionAgentCore(
  {
    username: "alice-agent",
    displayName: "Alice Agent",
    profile,
    spaceId,
    channelId,
    runtime: { kind: "cli-local", harness: "claude-code" },
  },
  operator,
  serverHttp,
  agentCore,
  crypto,
  createHttpClient,
  keyStore,
);
```

For server-confirmed pairing, Web should call `startPairing`, send the user to
the returned `confirmUrl`, then poll with `pollPairingAndUseGrant(pairingId)`.
Only a confirmed poll installs the returned local grant.

## Required Web Flow

1. Web loads the current user's `StoredIdentity` from `BrowserKeyStore`.
2. Web creates or obtains the agent identity certificate and operator
   attestation using existing browser crypto.
3. Web registers that cert/attestation material with the Puffo server.
4. Web calls local `POST /agents` with `coreIdentity.source = "web_signed"` and
   only public metadata.
5. Web authenticates new local management calls with a daemon-issued local
   grant from the server-confirmed pairing flow or from exchanging the printed
   local control token, not the old signed `x-puffo-*` local bridge auth.
6. Web may create the local agent as a draft first. If it asks for `start:
   true`, the local daemon still needs the Rust core to open a native session
   for that agent identity.

## Important Gap

Web-signed metadata does not by itself provision native agent/session key
material. Production message receive/send still requires one of these follow-up
paths:

- native pairing/restore provisions the operator and agent identity material in
  the Rust core store;
- Web performs an explicit import into Rust core through a new, audited local
  API that accepts the Node heap exposure tradeoff;
- Web signs/registers the association for MVP, while production message
  transport remains disabled until native bootstrap is complete.

The current implementation chooses the first and third-compatible boundary:
metadata-only attachment now, native restore/import as a separate decision.
