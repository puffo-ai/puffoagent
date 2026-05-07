# Agent Core Local API

This is the localhost contract exposed by the `agent-core` daemon for the Web
app. The daemon binds loopback only, normally `http://127.0.0.1:63387`.

## CLI Output Contract

`agent start` prints a stable three-line connection block that Web/bootstrap
flows may parse:

```text
agent daemon listening on http://127.0.0.1:63387
local control token: <token>
Return to Web and click Re-check.
```

When a daemon is already running, the first line uses
`agent daemon already listening` with the existing URL and the same token line.
The token is local-only bootstrap material; Web should exchange it for a
short-lived local grant when possible and avoid storing it long term.

Machine-readable bootstrap callers can use:

```bash
agent start --json
```

Response:

```json
{
  "status": "listening",
  "ok": true,
  "version": "0.1.0",
  "host": "127.0.0.1",
  "port": 63387,
  "url": "http://127.0.0.1:63387",
  "token": "local-control-token",
  "authRequired": true,
  "message": "Return to Web and click Re-check."
}
```

If the daemon was already running, `status` is `"already_listening"` and the
same existing daemon URL/token are returned.

## Browser Access

The daemon allows browser localhost calls:

```text
Access-Control-Allow-Origin: *
Access-Control-Allow-Methods: GET,POST,DELETE,OPTIONS
Access-Control-Allow-Headers: Content-Type,Authorization,X-Agent-Core-Token,X-Agent-Core-Account-Id,X-Agent-Core-Operator-Slug
Access-Control-Allow-Private-Network: true
Cache-Control: no-store
X-Content-Type-Options: nosniff
```

By default the origin policy stays permissive for MVP Web discovery. Set
`AGENT_CORE_ALLOWED_ORIGINS` to a comma-separated list of exact HTTP(S) origins
to restrict browser callers:

```bash
AGENT_CORE_ALLOWED_ORIGINS=https://app.example.test,http://localhost:3000 agent start
```

When this is configured, matching browser requests receive
`Access-Control-Allow-Origin: <request-origin>`, non-matching browser origins
receive `403`, and local non-browser clients without an `Origin` header still
work through the normal token authorization path.

Discovery routes are public. The server-confirmed pairing start/poll routes are
also public on loopback so a logged-in Web page can connect a fresh daemon
before it has a local grant. Management routes require local authorization. The
MVP fallback path is the local control token printed by `agent start`.

Legacy Web discovery compatibility:

```http
GET /v1/info
```

This read-only route is intentionally limited to the old bridge discovery
shape. It lets existing Web code that probes `/v1/info` detect that the new
daemon is running, but it does not implement the previous signed `/v1/pair` or
secret-bundle `/v1/agents` bridge.

Response:

```json
{
  "service": "puffo-agent-bridge",
  "version": "v1",
  "daemon_version": "0.1.0",
  "runtime": "agent-core",
  "pid": 12345,
  "hostname": "Alice-MacBook.local",
  "agent_count": 0,
  "paired": false,
  "paired_slug": null,
  "paired_device_id": null
}
```

```http
Authorization: Bearer <token>
```

or:

```http
X-Agent-Core-Token: <token>
```

If both supported headers are present, the daemon treats them as alternatives
and authorizes the request when any presented token is valid. Grant-management
routes still require the local control token specifically.

Web management requests should also include the current logged-in account
context:

```http
X-Agent-Core-Account-Id: <current-account-id>
X-Agent-Core-Operator-Slug: <current-operator-slug>
```

`X-Agent-Core-Account-Id` must be at most 256 characters. `X-Agent-Core-Operator-Slug`
must be a lowercase core slug. Malformed account-context headers fail with
`400 bad_request`; valid headers for a different local binding fail with
`409 account_mismatch`.

When the daemon has a stored account binding, account-bound local grants require
matching account headers. A request that presents the local control token with
different current-account headers fails with `409 account_mismatch` instead of
managing another account's local daemon state.

The local control token can mint short-lived scoped local grants for the Web
management UI. Grants are stored hashed in `device.json`, expire locally, and
can be revoked by the daemon. A local grant can call management routes but
cannot mint or revoke other grants.

The local control token can also rotate itself. Rotation returns a new control
token, immediately invalidates the old control token, and clears existing local
grants. This is the local recovery path if the printed token was copied into
the wrong place or a Web session should be fully disconnected.

POST routes that send a non-empty JSON body must use
`Content-Type: application/json`. When a route accepts a JSON body, the body
must be a JSON object; arrays, `null`, and primitive JSON values are rejected.

## Control Token Rotation

This route requires the current local control token; scoped local grants are
intentionally not accepted here.

```http
POST /local-control-token/rotate
```

The request body must be empty.

Response:

```json
{
  "token": "new-local-control-token",
  "rotated": true,
  "grantsRevoked": true
}
```

After this response, callers must use the returned token. All previously minted
local grants fail with `401`.

The terminal equivalent is:

```bash
agent rotate-token
```

When the daemon is running, the command calls the localhost API so the daemon's
in-memory token updates immediately. When the daemon is stopped, it rotates the
local `device.json` state directly.

## Local Grants

These routes require the local control token printed by `agent start`; scoped
local grants are intentionally not accepted here.

```http
GET /local-grants
```

Response:

```json
{
  "grants": [
    {
      "id": "1a60136f-88f0-4e7a-8d3a-6b748070c6fe",
      "scopes": ["management"],
      "createdAt": "2026-05-05T20:16:24.000Z",
      "expiresAt": "2026-05-05T20:31:24.000Z",
      "active": true
    }
  ]
}
```

The list only returns grant metadata; local grant tokens and token hashes are
never returned after minting.

```http
POST /local-grants
```

Request:

```json
{
  "ttlMs": 900000
}
```

`ttlMs` is optional, defaults to 15 minutes, and must be between 1 second and
24 hours. The body must be a JSON object and unknown fields are rejected.

Response:

```json
{
  "id": "1a60136f-88f0-4e7a-8d3a-6b748070c6fe",
  "token": "grant-token-returned-once",
  "scopes": ["management"],
  "expiresAt": "2026-05-05T20:31:24.000Z"
}
```

The returned token can be used with the same headers as the local control
token:

```http
Authorization: Bearer <grant-token>
```

or:

```http
X-Agent-Core-Token: <grant-token>
```

Revoke a grant with the local control token:

```http
DELETE /local-grants/:id
```

The request body must be empty.

Response:

```json
{
  "id": "1a60136f-88f0-4e7a-8d3a-6b748070c6fe",
  "revoked": true
}
```

## Discovery

```http
GET /health
GET /configuration?accountId=<current-account-id>&operatorSlug=<current-operator-slug>
```

Response:

```json
{
  "ok": true,
  "version": "0.1.0",
  "authRequired": true,
  "instanceId": "daemon-session-id",
  "binding": {
    "accountId": "acct_alice",
    "operatorSlug": "alice",
    "deviceId": "native-sidecar-prod",
    "pairedAt": "2026-05-07T12:00:00.000Z"
  }
}
```

When the request includes the valid local control token, the response also
includes `stateHome`. Public Web discovery should not rely on it.

`/configuration` is the account-aware setup probe. Web must not treat
`/health.ok`, native `core.status`, or `authTokenSource` as proof that the
daemon is configured for the current Web account. Use `/configuration` and
compare the logged-in account instead:

The query string is strict: only `accountId` and `operatorSlug` are accepted,
each at most once. `accountId` is optional and bounded to 256 characters;
`operatorSlug` is optional and must be a lowercase core slug. Malformed setup
probe queries return `400 bad_request` so Web does not silently compare the
wrong account context.

```json
{
  "daemonAvailable": true,
  "state": "configured_for_different_account",
  "configured": false,
  "binding": {
    "accountId": "acct_alice",
    "operatorSlug": "alice",
    "deviceId": "native-sidecar-prod",
    "pairedAt": "2026-05-07T12:00:00.000Z"
  },
  "current": {
    "accountId": "acct_bob",
    "operatorSlug": "bob"
  }
}
```

Expected setup states:

- no daemon/runtime available: the localhost probe cannot connect;
- daemon available but no stored binding: `state = "not_configured"`;
- daemon bound to current Web account: `state = "configured_for_current_account"`;
- daemon bound to another Web account: `state = "configured_for_different_account"`.

```json
{
  "ok": true,
  "version": "0.1.0",
  "authRequired": true,
  "instanceId": "daemon-session-id",
  "stateHome": "/Users/alice/.agent-core"
}
```

```http
GET /providers
```

Returns OS, Node version, product server reachability, macOS sandbox support,
and provider readiness. Public discovery hides local executable paths; tokened
requests include them for diagnostics. If a detected provider executable fails
its `--version` probe, the provider is reported as `ready: false` with
`reason: "crashed"` and a `fixCommand` that Web can show as a repair step.

```json
{
  "os": "darwin",
  "arch": "arm64",
  "nodeVersion": "v25.4.0",
  "server": {
    "url": "https://api.puffo.ai",
    "status": "reachable",
    "reachable": true
  },
  "sandbox": {
    "supported": true,
    "provider": "sandbox-exec"
  },
  "providers": {
    "claude": {
      "provider": "claude",
      "installed": true,
      "ready": true,
      "version": "2.1.121 (Claude Code)",
      "authStatus": "ready"
    },
    "codex": {
      "provider": "codex",
      "installed": true,
      "ready": true,
      "version": "codex-cli 0.128.0",
      "authStatus": "ready"
    }
  }
}
```

## Pairing

These routes drive the backend PR #26 pairing contract when the daemon is
started normally. If the API server is constructed without the server-pairing
gateway, they fall back to the native core's unavailable pairing response.
`AGENT_CORE_SERVER_URL` defaults to `https://api.puffo.ai`; custom values must
be HTTPS, except loopback HTTP is accepted for local development. Values with
embedded credentials, query strings, or fragments are rejected before any
request is made. Successful backend pairing responses must use
`application/json` or another `+json` media type, and redirects are not
followed.

```http
POST /pairing/start
GET /pairing/:pairingId
POST /pairing/confirm
```

`POST /pairing/start` and `GET /pairing/:pairingId` do not require a local
authorization token; they are limited by loopback Host validation, trusted
browser Origin checks, strict request/response schemas, and the product
server's authenticated confirmation step. Direct `POST /pairing/confirm`
remains a protected management route because it accepts a server-issued
`authToken`. By default, browser pairing start/poll requests are accepted only
from `https://chat.puffo.ai`, `https://app.puffo.ai`, and local dev origins on
ports 3000/5173; `AGENT_CORE_ALLOWED_ORIGINS` replaces that browser-origin
allowlist when configured. Non-browser local clients without an `Origin` header
can still run the start/poll flow from the terminal.

`POST /pairing/start` calls `POST /agent-core/pairings` on the product server
with a daemon-generated `pairingPublicNonce` unless one is supplied explicitly,
plus daemon version, platform, arch, and the local API origin derived from the
actual localhost request host. A supplied `pairingPublicNonce` must be an
unpadded base64url string from 32 to 128 characters. The local request body is
a strict schema: only `pairingPublicNonce` is accepted. Fields such as
`localApiOrigin`, `authToken`, `operatorBootstrap`, or arbitrary client metadata
are rejected before native core or backend sees the payload. The backend start
response is parsed as a strict schema; unknown top-level fields are rejected.
Response:

```json
{
  "status": "pending",
  "pairingId": "pair_...",
  "userCode": "ABCD-EFGH",
  "confirmUrl": "https://app.example/agents/pair?pairingId=pair_...",
  "expiresAt": "2026-05-06T19:00:00.000Z",
  "pollAfterMs": 1000
}
```

`GET /pairing/:pairingId` polls `GET /agent-core/pairings/:pairingId`. When the
server returns the one-time `authToken`, the daemon immediately hands it to
native `confirmPairing`; the local response never echoes that token. If the
server asks for a daemon-minted local Web grant, the response includes that
short-lived local grant only on the first confirmed poll, tied to the one-time
token handoff. A successful confirmed poll stores public local binding metadata
(`accountId`, `operatorSlug`, optional `deviceId`) and clears older local grants
before minting the new account-bound grant. The daemon only honors
`localWebGrant.mode = "daemon_mints"`
with an integer `ttlMs` between 1 second and 15 minutes; malformed or longer
grant hints are ignored.
`operatorSlug`, `accountId`, `authToken`, `operatorBootstrap`, and
`localWebGrant` are confirmed-only fields. Pending, expired, or canceled
backend poll responses that include them fail closed before native core or Web
sees the payload.
Confirmed backend poll responses are also parsed as a strict schema; unknown
top-level fields are rejected before the daemon forwards anything to native core
or Web. If the backend includes `operatorBootstrap.payload`, the daemon strips
that payload from the localhost response and returns only the Web-safe
`operatorBootstrap.kind` and `operatorBootstrap.operatorSlug` summary.

Manual `POST /pairing/confirm` is for native-side confirmation only. Its
optional `pairingId` must be the same safe identifier shape used by
`GET /pairing/:pairingId`; the local response must not include the submitted
`authToken`. The daemon trims the submitted `authToken` and rejects empty or
oversized values over 16 KiB before forwarding it to native core.

```json
{
  "status": "confirmed",
  "pairingId": "pair_...",
  "operatorSlug": "alice",
  "accountId": "acct_...",
  "operatorBootstrap": {
    "kind": "existing_local_identity",
    "operatorSlug": "alice"
  },
  "localGrant": {
    "id": "grant_...",
    "token": "local-grant-token",
    "scopes": ["management"],
    "expiresAt": "2026-05-06T19:00:00.000Z"
  },
  "core": {
    "connected": false,
    "status": "pairing_required",
    "authTokenSource": "keychain"
  }
}
```

Manual/provisional backend handoff is still available: when the native sidecar
is running in production profile, `POST /pairing/confirm` receives
`{ "authToken": "..." }`. The
sidecar activates the token in-process and, in a production macOS build with
`apple-keychain`, persists it in Keychain. Production builds fail the handoff if
Keychain persistence fails, and report Keychain read errors explicitly instead
of treating them as a missing token; dev/non-Keychain builds keep the token only
in sidecar memory. `POST /pairing/start` must receive a JSON object when a body
is present, and that body may contain only `pairingPublicNonce`. For
`POST /pairing/confirm`, `authToken` is required and must be a non-empty string;
if `pairingId` is present it must be a string safe pairing identifier. Unknown
fields, including `operatorBootstrap`, are rejected before reaching native core.
A successful local handoff returns the normal
production-profile core status without exposing the token:

```json
{
  "connected": false,
  "status": "pairing_required",
  "deviceId": "native-sidecar-prod",
  "mode": "prod",
  "serverUrl": "https://api.puffo.ai",
  "authTokenSource": "keychain",
  "reason": "production native profile parsed; backend PR #26 must merge/deploy for pairing and backend PR #25 must merge/deploy from dev before spaces/invites sync can be trusted",
  "blockedBy": [
    "backend_pairing_contract",
    "space_invite_sync_contract"
  ],
  "nextAction": "Merge/deploy backend PR #26 for pairing, then merge/deploy backend PR #25 from dev and run production agent identity publication plus account-bound sync verification."
}
```

If the production profile is missing required local configuration, core status
includes `missingConfig`, `blockedBy`, and `nextAction` so Web can show a
specific recovery step instead of a generic unavailable state:

```json
{
  "connected": false,
  "status": "unavailable",
  "deviceId": "native-sidecar-prod",
  "mode": "prod",
  "reason": "production native profile missing required configuration: AGENT_CORE_AUTH_TOKEN",
  "missingConfig": ["AGENT_CORE_AUTH_TOKEN"],
  "blockedBy": ["AGENT_CORE_AUTH_TOKEN"],
  "nextAction": "Confirm server pairing and pass the server-issued authToken to local /pairing/confirm."
}
```

If the native sidecar is missing or crashes before returning a structured
status, `/diagnostics` still returns a redacted core object with
`blockedBy: ["native_core_error"]` or `["native_core_unavailable"]` and a
`nextAction` that points the user toward `agent doctor` or a package/native
sidecar reinstall.

## Agents

```http
GET /agents
```

```json
{
  "agents": []
}
```

```http
POST /agents/preview
POST /agents
Content-Type: application/json
```

`POST /agents/preview` accepts the same body as agent creation and returns an
effective policy preview for a draft agent. It does not persist the agent,
create a core identity, start the provider, or project credentials.

Request:

```json
{
  "name": "Alice Agent",
  "provider": "codex",
  "accessMode": "safe",
  "networkAccess": "inherit",
  "deniedTools": ["security"],
  "fileAccess": {
    "readablePaths": ["/Users/alice/references"],
    "writablePaths": ["/Users/alice/output"]
  },
  "providerConfigPaths": [".codex/prompts", ".codex/skills"],
  "instructions": "Reply briefly.",
  "projectPath": "/Users/alice/project",
  "operatorSlug": "alice",
  "agentSlug": "alice-agent",
  "start": true
}
```

The create and preview bodies are parsed as strict schemas. Unknown top-level
fields are rejected. `fileAccess` is also strict and only accepts
`readablePaths` and `writablePaths`.

MVP Web-signed identity attachment can skip native identity creation by passing
identity metadata that Web has already signed/registered:

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

Fields:

| Field | Notes |
| --- | --- |
| `provider` | `claude` or `codex` |
| `accessMode` | `safe`, `project`, or `trusted`; default `safe`. On macOS, `safe` and `project` run in the generated sandbox by default; `trusted` skips it |
| `projectPath` | Required for `project`; must be an absolute existing directory and is canonicalized with `realpath` |
| `networkAccess` | `inherit` or `deny`; `deny` requires `safe` or `project` |
| `deniedTools` | Extra executable names or absolute paths blocked by the generated macOS sandbox profile |
| `fileAccess` | Extra filesystem resources for `safe` or `project` agents. `readablePaths` and `writablePaths` must be arrays of absolute existing directories and are canonicalized with `realpath`; writable paths are also readable inside the generated sandbox |
| `providerConfigPaths` | Extra provider config, MCP, command, prompt, or skill paths to copy from the user's real home into the isolated agent home. Entries must be supported provider-owned relative paths; examples include `.claude/commands`, `.claude/skills`, `.codex/prompts`, and `.codex/skills`. Broad roots such as `.claude` or `.codex` are refused; symlinks are refused |
| `instructions` | Optional string appended to provider prompts |
| `operatorSlug` | Enables native local core agent identity creation. Actual native create requests fail before persistence if the native core is not `ready` or cannot create the identity |
| `agentSlug` | Optional explicit core agent slug for native creation. Requires `operatorSlug` unless `coreIdentity` is supplied; when both are supplied it must match `coreIdentity.agentSlug` |
| `coreIdentity` | Optional already-created agent identity metadata. MVP Web can use this after signing/registering the agent association with the browser-held operator identity; `declaredOperatorPublicKey` is required as the public operator anchor from the signed agent IdentityCert and must be 4096 characters or less after trimming; the daemon accepts only the public metadata fields shown above, rejects unsupported fields such as secret-key bundles, persists only high-level metadata, and marks missing `source` as `web_signed`. Local API callers may only omit `source` or send `source: "web_signed"`; `source: "native"` is reserved for identities created by the Rust native path |
| `start` | Optional boolean; starts the provider session immediately after creation. `start: true` requires either `operatorSlug` or `coreIdentity` so the runtime can attach the product message loop instead of only starting the local provider process |

```http
GET /agents/:id
DELETE /agents/:id
POST /agents/:id/start
POST /agents/:id/stop
POST /agents/:id/restart
POST /agents/:id/reset-session
POST /agents/:id/recheck
```

Agent ids are opaque local ids generated by the daemon. Route ids must be 1-128
characters and contain only ASCII letters, digits, `_`, or `-`.

`DELETE /agents/:id` stops the local runtime and deletes this agent's local
state/workspace/session/log files. It does not delete `projectPath`. The
request body must be empty.

`POST /agents/:id/recheck` re-runs readiness detection for that agent's selected
provider and returns:

```json
{
  "provider": {
    "provider": "codex",
    "installed": true,
    "ready": true,
    "version": "codex-cli 0.128.0"
  }
}
```

`POST /agents/:id/start` and `/restart` require the agent to already have a
`coreIdentity`, either created natively from `operatorSlug` on `POST /agents` or
attached from a Web-signed/registered MVP flow. Agents created without
`operatorSlug` or `coreIdentity` can be inspected, edited, or deleted as local
drafts, but the local API will not start them because they cannot receive or
send product messages. Attaching `coreIdentity` avoids native identity creation
at create time; production start still needs a native core session that can open
that identity. If a legacy no-identity agent is already marked `running`, actual
policy updates are also refused because they would implicitly restart the local
provider process; policy previews remain allowed.
Agent action routes `start`, `stop`, `restart`, `reset-session`, and `recheck`
require an empty request body.

## Policy

```http
GET /agents/:id/policy
POST /agents/:id/policy?preview=true
POST /agents/:id/policy
Content-Type: application/json
```

`GET /agents/:id/policy` returns the persisted agent config plus a side-effect
free preview of the effective launch policy. It does not return provider
environment variables and does not project credentials into the isolated agent
home.
If a persisted project policy can no longer be resolved, for example because
the project directory was deleted, the route returns `400 bad_request` with a
`projectPath` message so Web can prompt the user to choose a new folder.
If the daemon's isolated agent home or workspace has been replaced with an
unsafe filesystem path such as a symlink, the same route returns
`400 bad_request` and the agent should be recreated or repaired locally.
`POST /agents/:id/policy?preview=true` accepts the same request body as a
policy update, validates it, and returns the proposed effective policy without
persisting the change or restarting a running agent. The policy query string is
strict: only `preview` is accepted, at most once, and it must be `true`,
`false`, `1`, or `0`.

Response:

```json
{
  "agent": {
    "id": "uuid",
    "name": "Alice Agent",
    "provider": "codex",
    "accessMode": "project"
  },
  "policy": {
    "accessMode": "project",
    "cwd": "/Users/alice/project",
    "agentHome": "/Users/alice/.agent-core/agents/uuid/home",
    "workspace": "/Users/alice/.agent-core/agents/uuid/workspace",
    "projectPath": "/Users/alice/project",
    "networkAccess": "deny",
    "deniedTools": ["security", "ps"],
    "fileAccess": {
      "readablePaths": ["/Users/alice/references"],
      "writablePaths": ["/Users/alice/output"]
    },
    "providerConfigPaths": [".codex/prompts", ".codex/skills"]
  }
}
```

Request:

```json
{
  "accessMode": "project",
  "projectPath": "/Users/alice/project",
  "networkAccess": "deny",
  "deniedTools": ["security", "ps"],
  "fileAccess": {
    "readablePaths": ["/Users/alice/references"],
    "writablePaths": ["/Users/alice/output"]
  },
  "providerConfigPaths": [".codex/prompts", ".codex/skills"]
}
```

If the agent is running, the runtime restarts it so the new provider
environment and sandbox profile take effect.
Send `"fileAccess": null` in a policy update to clear all extra file resource
grants, and `"providerConfigPaths": null` to clear all extra provider config
projection paths. `trusted` agents cannot use `networkAccess: "deny"`,
`deniedTools`, `fileAccess`, or `providerConfigPaths` because they do not run
under the restrictive sandbox. Policy update bodies are strict schemas:
unknown top-level fields are rejected, and `fileAccess` only accepts
`readablePaths` and `writablePaths`.

## Status And Logs

```http
GET /agents/:id/status
```

Response includes persisted config plus live runtime attachment:

```json
{
  "agent": {
    "id": "uuid",
    "name": "Alice Agent",
    "provider": "codex",
    "accessMode": "safe",
    "status": "running"
  },
  "runtime": {
    "attached": true,
    "providerStatus": { "state": "ready" },
    "coreSessionOpen": true,
    "messageLoopOpen": true,
    "pollerActive": true,
    "tickInProgress": false
  }
}
```

Any `providerStatus.lastError` value is redacted before it leaves the daemon.

```http
GET /agents/:id/logs
GET /agents/:id/logs?maxLines=50
```

Returns local log lines. `maxLines` is optional, defaults to 200, and must be an
integer from 0 to 1000. The logs query string is strict: only `maxLines` is
accepted, at most once. Lines are redacted again at read time, so legacy log
content is not returned with raw tokens or keys:

```json
{
  "lines": ["2026-05-06T12:00:00.000Z started agent runtime"]
}
```

## Diagnostics

```http
GET /diagnostics
```

Returns health, native core status, environment/provider detection, and all
agent configs in one call.

## Dev-Only Message Injection

Disabled unless `AGENT_CORE_DEV_ROUTES=1`.

```http
POST /agents/:id/dev-inject
Content-Type: application/json
```

```json
{
  "senderSlug": "alice",
  "body": "@alice-agent status?"
}
```

This injects a dev encrypted channel message into the Rust sidecar and ticks the
agent once. The body is a strict schema: `body` is required, `senderSlug` is
optional and must be a lowercase core slug, and unknown fields are rejected. It
is only for local smoke tests before production transport is wired.

## Error Shape

Errors are JSON:

```json
{
  "error": {
    "code": "bad_request",
    "message": "projectPath must be an existing directory"
  }
}
```

Common codes:

| HTTP | Code |
| --- | --- |
| 400 | `bad_request` |
| 401 | `unauthorized` |
| 403 | `forbidden` |
| 404 | `not_found` |
| 413 | `payload_too_large` |
| 415 | `unsupported_media_type` |
| 500 | `internal_error` |
