# Agent Core Review Handoff

This is the short reviewer handoff for the local agent-core MVP.

## Current Local State

- Parent workspace branch: `feature/agent-core-local-mvp`
- Parent workspace commit: run `git rev-parse --short HEAD` from this branch
- Parent workspace remote: `https://github.com/puffo-ai/puffoagent.git`
- Parent review PR: https://github.com/puffo-ai/puffoagent/pull/1
- Web submodule branch: `feature/agent-core-web-signed-mvp`
- Web submodule commit: `165c6a0 feat(web): add agent core web-signed handoff`

The parent commit records:

- `agent-core/core` at core PR #18 commit `ece389a...`
- `puffo-core-han-group` at Web PR #52 commit `165c6a0...`

For offline review, a git bundle can be produced with:

```bash
mkdir -p /Users/glimmer/Desktop/projects/puffo.ai/handoff
git bundle create \
  /Users/glimmer/Desktop/projects/puffo.ai/handoff/agent-core-local-mvp.bundle \
  feature/agent-core-local-mvp
git bundle verify /Users/glimmer/Desktop/projects/puffo.ai/handoff/agent-core-local-mvp.bundle
shasum -a 256 /Users/glimmer/Desktop/projects/puffo.ai/handoff/agent-core-local-mvp.bundle
```

## Pull Requests

| Area | PR | State |
| --- | --- | --- |
| Parent runtime integration | https://github.com/puffo-ai/puffoagent/pull/1 | Open, clean, CI green |
| Rust core native bridge | https://github.com/puffo-ai/core/pull/18 | Open, clean |
| Backend signer ids / invite proof | https://github.com/puffo-ai/puffo-server/pull/25 | Open, clean, CI green |
| Backend daemon pairing contract | https://github.com/puffo-ai/puffo-server/pull/26 | Open, clean, no reported checks |
| Web non-UI handoff | https://github.com/puffo-ai/puffo-core-han-group/pull/52 | Open, merge state unknown, CI green |

## Prompt-To-Artifact Checklist

| Requirement | Artifact | Evidence |
| --- | --- | --- |
| Rewrite cli-local as Node + Rust | `agent-core/package.json`, `agent-core/src/**`, `agent-core/crates/agent-native/**` | `npm test` passes with 206 tests |
| Keep Rust core as crypto/client boundary | `agent-core/core` submodule, `agent-core/src/native/**`, `agent-core/crates/agent-native/src/lib.rs` | `npm run test:native` and `npm run check:core-patch` pass |
| Support Claude/Codex CLI sessions | `agent-core/src/providers/claude/**`, `agent-core/src/providers/codex/**` | Provider/session tests covered by full `npm test` |
| Add macOS sandbox, file/network/tool policy | `agent-core/src/policy/**`, `agent-core/src/providers/process/child-process.ts` | Policy resolver and sandbox tests covered by full `npm test` |
| Add localhost daemon API and local grants | `agent-core/src/api/**`, `agent-core/src/daemon/**`, `agent-core/src/state/**` | API/daemon/state tests covered by full `npm test` |
| Add server-confirmed pairing gateway | `agent-core/src/pairing/server-pairing.ts`, local `/pairing/*` routes | API pairing tests covered by full `npm test`; backend PR #26 still must merge/deploy |
| Keep MVP Web signing path | `puffo-core-han-group/client/web/src/agent-core/**` | Web PR #52 is green; focused Web tests pass with 9 tests |
| Publishable one-line install package | `agent-core/scripts/bootstrap-macos.sh`, `agent-core/package.json` | `npm run check:package`, `npm run smoke:package`, and prod `npm publish --dry-run` pass |
| Avoid occupied unscoped npm package | package name `@puffo-ai/agent-core`, bin `agent` | npm dry-run confirms public scoped package; actual publish needs npm login/org access |

## Verification Commands Already Run

From `agent-core/`:

```bash
npm test
npm run test:native
npm run check:core-patch
npm run check:package
npm run smoke:package
AGENT_CORE_NATIVE_BUILD_PROFILE=prod npm publish --dry-run
npm run build:native:dev
```

From `puffo-core-han-group/client/web/`:

```bash
npm test -- tests/agent-core-client.test.ts tests/agent-core-provision.test.ts
npm run typecheck
npm test
```

Repository hygiene:

```bash
git diff --check
find agent-core -maxdepth 1 -name '*.tgz' -print
```

## Agent-Core CI Note

An `agent-core` GitHub Actions workflow should be added once a token with
`workflow` scope is available. The attempted workflow push was rejected by
GitHub because this machine's OAuth token cannot create or update
`.github/workflows/*`.

The proposed workflow should run these commands on macOS:

```bash
(cd agent-core && npm run check:core-patch)
(cd agent-core && npm test)
(cd agent-core && npm run check:package)
(cd agent-core && npm run smoke:package)
```

## Current Blockers

The implementation is ready for review, but production completion is blocked by:

1. `npm whoami --registry=https://registry.npmjs.org/` returns `ENEEDAUTH`,
   so `@puffo-ai/agent-core` cannot be published from this machine.
2. Core PR #18 must merge, then the parent `agent-core/core` submodule pointer
   should move from the PR branch commit to the merged upstream revision.
3. Backend PR #25 and PR #26 must merge/deploy from `dev` before production
   pairing/message transport can be verified end to end.
4. Product/backend must decide whether confirmed daemon pairing should mint,
   rotate, or revoke scoped local Web grants automatically.
