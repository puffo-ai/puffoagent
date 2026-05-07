# Agent Core Owner Next Steps

This is the minimal handoff for someone with repository, workflow, npm, and
deployment permissions.

## 1. Add Agent-Core CI

Current blocker: the implementation token does not have GitHub `workflow`
scope.

Copy the proposed workflow from the local handoff into the canonical parent
repo:

```text
/Users/glimmer/Desktop/projects/puffo.ai/handoff/agent-core-workflow.yml
```

Destination:

```text
.github/workflows/agent-core.yml
```

Current SHA256:

```text
f6b79baacbe1179c33ff65fc59057a021f6dffbe1b7868aa90ea2b7a45c560f8
```

The workflow runs:

```bash
npm run check:core-patch
npm test
npm run test:native
npm run check:package
npm run smoke:package
AGENT_CORE_NATIVE_BUILD_PROFILE=prod npm run smoke:package
AGENT_CORE_NATIVE_BUILD_PROFILE=prod npm publish --dry-run --registry=https://registry.npmjs.org/
```

## 2. Review And Merge PRs

Recommended order:

```text
1. https://github.com/puffo-ai/core/pull/18
2. https://github.com/puffo-ai/puffo-server/pull/25
3. https://github.com/puffo-ai/puffo-server/pull/26
4. https://github.com/puffo-ai/puffo-core-han-group/pull/52
5. https://github.com/puffo-ai/puffoagent/pull/1
```

After `core` PR #18 merges, update the parent `agent-core/core` submodule
pointer from the PR-branch commit to the merged upstream commit, then rerun the
agent-core gates before merging the parent PR.

## 3. Publish Package

Current blocker: this machine is not logged in to npm.

Expected checks:

```bash
npm whoami --registry=https://registry.npmjs.org/
npm view @puffo-ai/agent-core version --registry=https://registry.npmjs.org/
```

Publish from `agent-core/` only after the prod profile gates pass:

```bash
npm ci
npm test
npm run test:native
npm run check:core-patch
npm run check:package
npm run smoke:package
AGENT_CORE_NATIVE_BUILD_PROFILE=prod npm run smoke:package
AGENT_CORE_NATIVE_BUILD_PROFILE=prod npm publish --dry-run --registry=https://registry.npmjs.org/
AGENT_CORE_NATIVE_BUILD_PROFILE=prod npm publish --registry=https://registry.npmjs.org/
```

## 4. Deploy And Verify Production E2E

After backend PR #25 and #26 are merged into `dev` and deployed, verify:

```text
1. Web can start daemon pairing through localhost /pairing/start.
2. Web confirmation returns a server-confirmed local grant.
3. Native sidecar persists pairing material through the Rust core boundary.
4. Web-signed MVP can create a local draft agent without sending private keys to Node.
5. Claude and Codex agents can start from the local daemon.
6. Message receive -> provider response -> encrypted reply works against deployed backend.
7. Restarting the daemon resumes only agents with usable coreIdentity/session state.
8. Safe/project policy runs provider processes under macOS sandbox on macOS.
```

If production message transport is still blocked, keep the Web-signed path as a
local draft/control MVP and leave live start disabled until native session
bootstrap is proven.
