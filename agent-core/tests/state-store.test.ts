import assert from "node:assert/strict";
import os from "node:os";
import path from "node:path";
import fs from "node:fs/promises";
import test from "node:test";
import { defaultStateHome } from "../src/platform/paths.js";
import { StateStore } from "../src/state/store.js";
import { LogStore } from "../src/logs/log-store.js";
import { RuntimeManager } from "../src/runtime/runtime-manager.js";
import { CoreNative, OpenedAgentMessage } from "../src/native/core.js";
import { ProviderSession } from "../src/providers/provider-types.js";
import { AgentInput, AgentOutput, ProviderStatus } from "../src/types.js";
import { ResolvedPolicy } from "../src/policy/policy.js";

test("StateStore normalizes configured state roots to absolute paths", () => {
  const store = new StateStore("relative-agent-home");

  assert.equal(store.paths.root, path.resolve("relative-agent-home"));
  assert.equal(
    defaultStateHome({ AGENT_CORE_HOME: "~/agent-core-home-test" } as NodeJS.ProcessEnv),
    path.join(os.homedir(), "agent-core-home-test"),
  );
});

test("StateStore creates and persists agent config", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-state-"));
  const store = new StateStore(root);
  await store.init();

  const agent = await store.createAgent({
    name: "Sam Assistant",
    provider: "claude",
    accessMode: "project",
    networkAccess: "deny",
    deniedTools: ["python", "node", "python"],
    fileAccess: {
      readablePaths: [root, root],
      writablePaths: [root],
    },
    providerConfigPaths: [".claude/commands", ".claude/commands"],
    projectPath: root,
    instructions: "Be concise.",
  });

  assert.equal(agent.name, "Sam Assistant");
  assert.equal(agent.provider, "claude");
  assert.equal(agent.accessMode, "project");
  assert.equal(agent.networkAccess, "deny");
  assert.deepEqual(agent.deniedTools, ["python", "node"]);
  assert.deepEqual(agent.fileAccess, { readablePaths: [root], writablePaths: [root] });
  assert.deepEqual(agent.providerConfigPaths, [".claude/commands"]);
  assert.equal(agent.projectPath, root);

  const loaded = await store.getAgent(agent.id);
  assert.deepEqual(loaded, agent);

  const agents = await store.listAgents();
  assert.equal(agents.length, 1);
  assert.equal(agents[0]?.id, agent.id);

  if (process.platform !== "win32") {
    assert.equal((await fs.stat(agent.workspace)).mode & 0o777, 0o700);
  }
});

test("StateStore normalizes invalid access mode to safe", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-state-access-"));
  const store = new StateStore(root);
  await store.init();

  const agent = await store.createAgent({
    name: "Mode Agent",
    provider: "codex",
    accessMode: "invalid" as any,
  });

  assert.equal(agent.accessMode, "safe");
});

test("StateStore normalizes legacy agent config on read", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-state-legacy-"));
  const store = new StateStore(root);
  await store.init();
  const agent = await store.createAgent({
    name: "Legacy Agent",
    provider: "codex",
    accessMode: "safe",
  });
  const file = path.join(root, "agents", agent.id, "agent.json");
  const raw = JSON.parse(await fs.readFile(file, "utf8"));
  delete raw.networkAccess;
  delete raw.deniedTools;
  delete raw.fileAccess;
  delete raw.providerConfigPaths;
  raw.accessMode = "invalid";
  await fs.writeFile(file, `${JSON.stringify(raw, null, 2)}\n`);

  const loaded = await store.getAgent(agent.id);
  assert.equal(loaded?.accessMode, "safe");
  assert.equal(loaded?.networkAccess, "inherit");
  assert.deepEqual(loaded?.deniedTools, []);
  assert.deepEqual(loaded?.fileAccess, { readablePaths: [], writablePaths: [] });
  assert.deepEqual(loaded?.providerConfigPaths, []);
});

test("StateStore drops incomplete legacy core identity metadata", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-state-legacy-core-identity-"));
  const store = new StateStore(root);
  await store.init();
  const agent = await store.createAgent({
    name: "Legacy Identity Agent",
    provider: "codex",
  });
  agent.coreIdentity = {
    operatorSlug: "alice",
    agentSlug: "legacy-agent",
    identityType: "agent",
  } as any;
  await store.saveAgent(agent);

  const loaded = await store.getAgent(agent.id);
  assert.equal(loaded?.coreIdentity, undefined);
});

test("StateStore normalizes missing or invalid core identity source", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-state-core-identity-source-"));
  const store = new StateStore(root);
  await store.init();
  const agent = await store.createAgent({
    name: "Source Agent",
    provider: "codex",
    coreIdentity: {
      operatorSlug: "alice",
      agentSlug: "source-agent",
      identityType: "agent",
      declaredOperatorPublicKey: " operator-pub ",
      source: "unexpected",
    } as any,
  });

  const loaded = await store.getAgent(agent.id);
  assert.equal(loaded?.coreIdentity?.declaredOperatorPublicKey, "operator-pub");
  assert.equal(loaded?.coreIdentity?.source, "web_signed");

  const oversized = await store.createAgent({
    name: "Oversized Source Agent",
    provider: "codex",
    coreIdentity: {
      operatorSlug: "alice",
      agentSlug: "oversized-source-agent",
      identityType: "agent",
      declaredOperatorPublicKey: "k".repeat(4097),
      source: "web_signed",
    },
  });
  const loadedOversized = await store.getAgent(oversized.id);
  assert.equal(loadedOversized?.coreIdentity, undefined);
});

test("StateStore drops malformed legacy core identity slugs", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-state-core-identity-slug-"));
  const store = new StateStore(root);
  await store.init();
  const agent = await store.createAgent({
    name: "Malformed Slug Agent",
    provider: "codex",
  });
  agent.coreIdentity = {
    operatorSlug: "Alice",
    agentSlug: "bad slug",
    identityType: "agent",
    declaredOperatorPublicKey: "operator-pub",
  } as any;
  await store.saveAgent(agent);

  const loaded = await store.getAgent(agent.id);
  assert.equal(loaded?.coreIdentity, undefined);
});

test("StateStore creates and reuses local control token", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-device-"));
  const store = new StateStore(root);
  await store.init();

  const first = await store.ensureDeviceState();
  const second = await store.ensureDeviceState();

  assert.equal(second.apiToken, first.apiToken);
  assert.match(first.apiToken, /^[A-Za-z0-9_-]+$/);
});

test("StateStore regenerates local control token when device state is corrupt", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-device-corrupt-"));
  const store = new StateStore(root);
  await store.init();
  const deviceFile = path.join(root, "device.json");
  await fs.writeFile(deviceFile, "{bad-json");

  const device = await store.ensureDeviceState();
  const saved = JSON.parse(await fs.readFile(deviceFile, "utf8"));

  assert.match(device.apiToken, /^[A-Za-z0-9_-]+$/);
  assert.equal(saved.apiToken, device.apiToken);
});

test("StateStore stores hashed local access grants and enforces expiry and revocation", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-device-grants-"));
  const store = new StateStore(root);
  await store.init();

  const grant = await store.createLocalAccessGrant({ ttlMs: 60_000 });
  const saved = await store.readDeviceState();

  assert.match(grant.token, /^[A-Za-z0-9_-]+$/);
  assert.equal(saved?.localGrants?.length, 1);
  assert.notEqual(saved?.localGrants?.[0]?.tokenHash, grant.token);
  assert.equal(saved?.localGrants?.[0]?.scopes.includes("management"), true);
  assert.equal(await store.verifyLocalAccessGrant(grant.token, "management"), true);
  assert.equal(await store.verifyLocalAccessGrant(`${grant.token}-wrong`, "management"), false);

  assert.equal(await store.revokeLocalAccessGrant(grant.id), true);
  assert.equal(await store.verifyLocalAccessGrant(grant.token, "management"), false);

  const expired = await store.createLocalAccessGrant({ ttlMs: 0 });
  assert.equal(await store.verifyLocalAccessGrant(expired.token, "management"), false);

  const fresh = await store.createLocalAccessGrant({ ttlMs: 60_000 });
  const pruned = await store.readDeviceState();
  assert.deepEqual(
    pruned?.localGrants?.map((entry) => entry.id),
    [fresh.id],
  );
});

test("StateStore preserves local access grants when reusing device state", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-device-grant-reuse-"));
  const store = new StateStore(root);
  await store.init();
  const grant = await store.createLocalAccessGrant({ ttlMs: 60_000 });

  const device = await store.ensureDeviceState();
  const listed = await store.listLocalAccessGrants();

  assert.equal(device.localGrants?.length, 1);
  assert.equal(await store.verifyLocalAccessGrant(grant.token), true);
  assert.deepEqual(listed, [
    {
      id: grant.id,
      scopes: ["management"],
      createdAt: device.localGrants?.[0]?.createdAt,
      expiresAt: grant.expiresAt,
      active: true,
    },
  ]);
  assert.equal("tokenHash" in listed[0]!, false);
});

test("StateStore rotates the local control token and clears local grants", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-device-token-rotate-"));
  const store = new StateStore(root);
  await store.init();
  const before = await store.ensureDeviceState();
  const grant = await store.createLocalAccessGrant({ ttlMs: 60_000 });

  const token = await store.rotateLocalControlToken();
  const after = await store.ensureDeviceState();

  assert.notEqual(token, before.apiToken);
  assert.equal(after.apiToken, token);
  assert.deepEqual(after.localGrants, []);
  assert.deepEqual(await store.listLocalAccessGrants(), []);
  assert.equal(await store.verifyLocalAccessGrant(grant.token), false);
});

test("StateStore ignores malformed legacy local access grants", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-device-grant-legacy-"));
  const store = new StateStore(root);
  await store.init();
  await store.ensureDeviceState();
  const deviceFile = path.join(root, "device.json");
  const raw = JSON.parse(await fs.readFile(deviceFile, "utf8"));
  raw.localGrants = [{ id: "broken", tokenHash: 42, scopes: ["management"] }];
  await fs.writeFile(deviceFile, `${JSON.stringify(raw, null, 2)}\n`);

  assert.equal(await store.verifyLocalAccessGrant("anything"), false);
  assert.equal(await store.revokeLocalAccessGrant("broken"), false);

  const grant = await store.createLocalAccessGrant({ ttlMs: 60_000 });
  const saved = await store.readDeviceState();
  assert.equal(saved?.localGrants?.length, 1);
  assert.equal(await store.verifyLocalAccessGrant(grant.token), true);
});

test("StateStore tightens permissions for existing state files", { skip: process.platform === "win32" }, async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-state-perms-"));
  const agentsDir = path.join(root, "agents");
  await fs.mkdir(agentsDir, { recursive: true, mode: 0o755 });
  const deviceFile = path.join(root, "device.json");
  await fs.writeFile(
    deviceFile,
    JSON.stringify({
      apiToken: "existing-token",
      createdAt: new Date().toISOString(),
      updatedAt: new Date().toISOString(),
    }),
    { mode: 0o644 },
  );
  await fs.chmod(root, 0o755);
  await fs.chmod(agentsDir, 0o755);
  await fs.chmod(deviceFile, 0o644);

  const store = new StateStore(root);
  const device = await store.ensureDeviceState();

  assert.equal(device.apiToken, "existing-token");
  assert.equal((await fs.stat(root)).mode & 0o777, 0o700);
  assert.equal((await fs.stat(agentsDir)).mode & 0o777, 0o700);
  assert.equal((await fs.stat(deviceFile)).mode & 0o777, 0o600);
});

test("StateStore tightens existing agent directories on write", { skip: process.platform === "win32" }, async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-state-agent-perms-"));
  const store = new StateStore(root);
  await store.init();
  const agent = await store.createAgent({
    name: "Perm Agent",
    provider: "codex",
  });
  const dir = path.join(root, "agents", agent.id);
  await fs.chmod(dir, 0o755);

  await store.saveAgent(agent);

  assert.equal((await fs.stat(dir)).mode & 0o777, 0o700);
  assert.equal((await fs.stat(path.join(dir, "agent.json"))).mode & 0o777, 0o600);
});

test("StateStore rejects symlinked state roots", async () => {
  const parent = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-state-root-parent-"));
  const outside = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-state-root-outside-"));
  const root = path.join(parent, "state-link");
  await fs.symlink(outside, root);

  const store = new StateStore(root);

  await assert.rejects(store.init(), /unsafe filesystem path/);
});

test("StateStore rejects symlinked state JSON files", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-state-file-symlink-"));
  const outside = path.join(root, "outside-device.json");
  const store = new StateStore(root);
  await store.init();
  await fs.writeFile(
    outside,
    JSON.stringify({
      apiToken: "external-token",
      createdAt: new Date().toISOString(),
      updatedAt: new Date().toISOString(),
    }),
  );
  await fs.symlink(outside, path.join(root, "device.json"));

  await assert.rejects(store.ensureDeviceState(), /unsafe filesystem path/);
});

test("StateStore does not write through symlinked agent directories", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-state-agent-symlink-"));
  const outside = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-state-agent-outside-"));
  const store = new StateStore(root);
  await store.init();
  const agent = await store.createAgent({
    name: "Symlink Agent",
    provider: "codex",
  });
  const dir = path.join(root, "agents", agent.id);
  await fs.rm(dir, { recursive: true, force: true });
  await fs.symlink(outside, dir);

  await assert.rejects(store.saveAgent({ ...agent, name: "Changed" }), /unsafe filesystem path/);
  await assert.rejects(fs.access(path.join(outside, "agent.json")), /ENOENT/);
});

test("StateStore does not read through symlinked agent directories", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-state-agent-read-symlink-"));
  const outside = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-state-agent-read-outside-"));
  const store = new StateStore(root);
  await store.init();
  const agent = await store.createAgent({
    name: "Read Symlink Agent",
    provider: "codex",
  });
  const outsideAgent = { ...agent, name: "External Agent" };
  await fs.writeFile(path.join(outside, "agent.json"), `${JSON.stringify(outsideAgent, null, 2)}\n`);
  const dir = path.join(root, "agents", agent.id);
  await fs.rm(dir, { recursive: true, force: true });
  await fs.symlink(outside, dir);

  await assert.rejects(store.getAgent(agent.id), /unsafe filesystem path/);
});

test("StateStore does not delete through symlinked agent directories", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-state-agent-delete-symlink-"));
  const outside = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-state-agent-delete-outside-"));
  const outsideCanary = path.join(outside, "canary.txt");
  const store = new StateStore(root);
  await store.init();
  const agent = await store.createAgent({
    name: "Delete Symlink Agent",
    provider: "codex",
  });
  await fs.writeFile(outsideCanary, "keep");
  const dir = path.join(root, "agents", agent.id);
  await fs.rm(dir, { recursive: true, force: true });
  await fs.symlink(outside, dir);

  await assert.rejects(store.deleteAgent(agent.id), /unsafe filesystem path/);
  assert.equal(await fs.readFile(outsideCanary, "utf8"), "keep");
});

test("StateStore uses collision-resistant temp files for concurrent writes", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-state-concurrent-"));
  const store = new StateStore(root);
  await store.init();
  const agent = await store.createAgent({
    name: "Concurrent Agent",
    provider: "codex",
  });

  await Promise.all(
    Array.from({ length: 10 }, async (_, index) => {
      const copy = { ...agent, instructions: `write-${index}` };
      await store.saveAgent(copy);
    }),
  );

  const loaded = await store.getAgent(agent.id);
  assert.match(loaded?.instructions ?? "", /^write-\d+$/);
  const dirEntries = await fs.readdir(path.join(root, "agents", agent.id));
  assert.equal(dirEntries.some((entry) => entry.endsWith(".tmp")), false);
});

test("StateStore deletes local agent state without touching project paths", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-state-delete-"));
  const project = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-project-delete-"));
  const store = new StateStore(root);
  await store.init();
  const agent = await store.createAgent({
    name: "Delete Agent",
    provider: "codex",
    accessMode: "project",
    projectPath: project,
  });

  await store.deleteAgent(agent.id);

  assert.equal(await store.getAgent(agent.id), undefined);
  await fs.access(project);
});

test("StateStore rejects unsafe agent ids at the state boundary", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-state-id-boundary-"));
  const store = new StateStore(root);
  await store.init();
  const agent = await store.createAgent({
    name: "Boundary Agent",
    provider: "codex",
  });
  await fs.writeFile(path.join(root, "canary.json"), "keep");

  await assert.rejects(store.getAgent("../canary"), /invalid agent id/);
  await assert.rejects(store.readSession("../canary"), /invalid agent id/);
  await assert.rejects(store.saveSession("../canary", {}), /invalid agent id/);
  await assert.rejects(store.deleteAgent("../canary"), /invalid agent id/);
  await assert.rejects(store.saveAgent({ ...agent, id: "../canary" }), /invalid agent id/);
  await assert.rejects(store.getAgent("a".repeat(129)), /invalid agent id/);

  assert.equal(await fs.readFile(path.join(root, "canary.json"), "utf8"), "keep");
  assert.equal((await store.getAgent(agent.id))?.id, agent.id);
});

test("StateStore listAgents skips invalid legacy directory names", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-state-list-invalid-"));
  const store = new StateStore(root);
  await store.init();
  const agent = await store.createAgent({
    name: "Valid Agent",
    provider: "codex",
  });
  await fs.mkdir(path.join(root, "agents", "bad.name"), { recursive: true });
  await fs.mkdir(path.join(root, "agents", "a".repeat(129)), { recursive: true });

  const agents = await store.listAgents();

  assert.equal(agents.length, 1);
  assert.equal(agents[0]?.id, agent.id);
});

test("StateStore listAgents skips corrupt legacy agent records", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-state-list-corrupt-"));
  const store = new StateStore(root);
  await store.init();
  const agent = await store.createAgent({
    name: "Valid Agent",
    provider: "codex",
  });
  const corruptId = "corrupt-agent";
  await fs.mkdir(path.join(root, "agents", corruptId), { recursive: true });
  await fs.writeFile(path.join(root, "agents", corruptId, "agent.json"), "{bad-json");

  const agents = await store.listAgents();

  assert.equal(agents.length, 1);
  assert.equal(agents[0]?.id, agent.id);
  await assert.rejects(store.getAgent(corruptId), /Unexpected token|JSON/);
});

test("RuntimeManager persists Claude session id on start", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-runtime-"));
  const store = new StateStore(root);
  await store.init();
  const runtime = new RuntimeManager(store, new LogStore(root));
  const agent = await runtime.createAgent({
    name: "Claude Agent",
    provider: "claude",
    accessMode: "safe",
  });

  await runtime.startAgent(agent.id);
  const session = await store.readSession<{ sessionId?: string }>(agent.id);

  assert.match(session?.sessionId ?? "", /^[0-9a-f-]{36}$/);
});

test("RuntimeManager reset-session restarts running Claude with a new persisted session id", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-runtime-reset-"));
  const store = new StateStore(root);
  await store.init();
  const runtime = new RuntimeManager(store, new LogStore(root));
  const agent = await runtime.createAgent({
    name: "Claude Agent",
    provider: "claude",
    accessMode: "safe",
  });

  await runtime.startAgent(agent.id);
  const before = await store.readSession<{ sessionId?: string }>(agent.id);
  const reset = await runtime.resetSession(agent.id);
  const after = await store.readSession<{ sessionId?: string }>(agent.id);

  assert.equal(reset.status, "running");
  assert.match(after?.sessionId ?? "", /^[0-9a-f-]{36}$/);
  assert.notEqual(after?.sessionId, before?.sessionId);
});

test("RuntimeManager policy updates restart a running agent with the new policy", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-runtime-policy-"));
  const store = new StateStore(root);
  await store.init();
  const sessions: CountingProviderSession[] = [];
  const policies: ResolvedPolicy[] = [];
  const runtime = new RuntimeManager(store, new LogStore(root), undefined, {
    providerFactory: (_provider, policy) => {
      policies.push(policy);
      const session = new CountingProviderSession();
      sessions.push(session);
      return session;
    },
  });
  const agent = await runtime.createAgent({
    name: "Policy Agent",
    provider: "codex",
    accessMode: "safe",
  });

  await runtime.startAgent(agent.id);
  const updated = await runtime.updateAgentPolicy(agent.id, {
    networkAccess: "deny",
    deniedTools: ["python"],
  });

  assert.equal(updated.status, "running");
  assert.equal(sessions.length, 2);
  assert.equal(sessions[0]?.stopped, true);
  assert.equal(sessions[1]?.started, true);
  assert.equal(policies[1]?.networkAccess, "deny");
  assert.deepEqual(policies[1]?.deniedTools, ["python"]);
});

test("RuntimeManager resolves provider executable path before launch", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-runtime-command-path-"));
  const bin = path.join(root, "bin");
  await fs.mkdir(bin, { recursive: true });
  const codexPath = path.join(bin, "codex");
  await fs.writeFile(codexPath, "#!/usr/bin/env sh\nexit 0\n", { mode: 0o755 });
  const previousPath = process.env.PATH;
  process.env.PATH = `${bin}${path.delimiter}${previousPath ?? ""}`;
  let commandPath: string | undefined;

  try {
    const store = new StateStore(root);
    await store.init();
    const runtime = new RuntimeManager(store, new LogStore(root), undefined, {
      providerFactory: (_provider, _policy, state) => {
        commandPath = state.commandPath;
        return new CountingProviderSession();
      },
    });
    const agent = await runtime.createAgent({
      name: "Codex Agent",
      provider: "codex",
      accessMode: "safe",
    });

    await runtime.startAgent(agent.id);

    assert.equal(commandPath, codexPath);
  } finally {
    if (previousPath === undefined) delete process.env.PATH;
    else process.env.PATH = previousPath;
  }
});

test("RuntimeManager stop is best-effort when provider stop fails", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-runtime-stop-fail-"));
  const store = new StateStore(root);
  await store.init();
  const runtime = new RuntimeManager(store, new LogStore(root), undefined, {
    providerFactory: () => new SecretStopFailProviderSession(),
  });
  const agent = await runtime.createAgent({
    name: "Stop Fail Agent",
    provider: "codex",
  });

  await runtime.startAgent(agent.id);
  const stopped = await runtime.stopAgent(agent.id);
  const status = await runtime.getAgentStatus(agent.id);

  assert.equal(stopped.status, "stopped");
  assert.equal(status.runtime.attached, false);
  assert.doesNotMatch(stopped.lastError ?? "", /secret-value/);
  assert.match(stopped.lastError ?? "", /token=\[redacted\]/);
});

test("RuntimeManager redacts provider status errors", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-runtime-status-redact-"));
  const store = new StateStore(root);
  await store.init();
  const runtime = new RuntimeManager(store, new LogStore(root), undefined, {
    providerFactory: () => new SecretStatusProviderSession(),
  });
  const agent = await runtime.createAgent({
    name: "Status Secret Agent",
    provider: "codex",
  });

  await runtime.startAgent(agent.id);
  const status = await runtime.getAgentStatus(agent.id);

  assert.equal(status.runtime.providerStatus?.state, "error");
  assert.doesNotMatch(status.runtime.providerStatus?.lastError ?? "", /secret-value/);
  assert.match(status.runtime.providerStatus?.lastError ?? "", /token=\[redacted\]/);
});

test("RuntimeManager delete removes local state even when provider stop fails", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-runtime-delete-stop-fail-"));
  const store = new StateStore(root);
  await store.init();
  const runtime = new RuntimeManager(store, new LogStore(root), undefined, {
    providerFactory: () => new SecretStopFailProviderSession(),
  });
  const agent = await runtime.createAgent({
    name: "Delete Stop Fail Agent",
    provider: "codex",
  });

  await runtime.startAgent(agent.id);
  const deleted = await runtime.deleteAgent(agent.id);

  assert.equal(deleted.deleted, true);
  assert.equal(await store.getAgent(agent.id), undefined);
});

test("RuntimeManager tick exits cleanly when agent state was deleted", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-runtime-tick-deleted-"));
  const store = new StateStore(root);
  await store.init();
  const runtime = new RuntimeManager(store, new LogStore(root), new ThrowingSyncCoreNative(), {
    autoStartMessageLoop: false,
  });
  const agent = await runtime.createAgent({
    name: "Deleted Tick Agent",
    provider: "codex",
    operatorSlug: "alice",
  });

  await runtime.startAgent(agent.id);
  await store.deleteAgent(agent.id);
  const handled = await runtime.tickAgent(agent.id);

  assert.equal(handled, 0);
  assert.equal(await store.getAgent(agent.id), undefined);
});

test("RuntimeManager persists provider session ids after message handling", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-runtime-provider-session-"));
  const store = new StateStore(root);
  await store.init();
  const core = new FakeCoreNative();
  const runtime = new RuntimeManager(store, new LogStore(root), core, {
    autoStartMessageLoop: false,
    providerFactory: () => new SessionReportingProviderSession("codex-session-1"),
  });
  const agent = await runtime.createAgent({
    name: "Codex Agent",
    provider: "codex",
    operatorSlug: "alice",
  });
  core.pending.push({
    id: "message-1",
    body: "@codex-agent status?",
    senderSlug: "alice",
    spaceId: "space-1",
    channelId: "channel-1",
    mentioned: true,
    dm: false,
    mustRespond: true,
  });

  await runtime.startAgent(agent.id);
  await runtime.tickAgent(agent.id);

  const session = await store.readSession<{ sessionId?: string }>(agent.id);
  assert.equal(session?.sessionId, "codex-session-1");
});

test("RuntimeManager creates core agent identity when operator slug is provided", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-identity-"));
  const store = new StateStore(root);
  await store.init();
  const core = new FakeCoreNative();
  const runtime = new RuntimeManager(store, new LogStore(root), core);

  const agent = await runtime.createAgent({
    name: "Sam Assistant",
    provider: "codex",
    operatorSlug: "alice",
  });

  assert.equal(agent.coreIdentity?.operatorSlug, "alice");
  assert.equal(agent.coreIdentity?.agentSlug, "sam-assistant");
  assert.equal(agent.coreIdentity?.identityType, "agent");

  const saved = await store.getAgent(agent.id);
  assert.equal(saved?.coreIdentity?.declaredOperatorPublicKey, "operator-pub");
});

test("RuntimeManager rejects native identities without a declared operator public key", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-identity-missing-operator-key-"));
  const store = new StateStore(root);
  await store.init();
  const runtime = new RuntimeManager(store, new LogStore(root), new MissingOperatorKeyCoreNative());

  await assert.rejects(
    runtime.createAgent({
      name: "Incomplete Native Agent",
      provider: "codex",
      operatorSlug: "alice",
    }),
    /declared operator public key/,
  );

  const agents = await store.listAgents();
  assert.equal(agents.length, 1);
  assert.equal(agents[0]?.status, "error");
  assert.match(agents[0]?.lastError ?? "", /declared operator public key/);
});

test("RuntimeManager rejects oversized declared operator public keys", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-identity-oversized-operator-key-"));
  const store = new StateStore(root);
  await store.init();
  const runtime = new RuntimeManager(store, new LogStore(root), new OversizedOperatorKeyCoreNative());

  await assert.rejects(
    runtime.createAgent({
      name: "Oversized Native Agent",
      provider: "codex",
      operatorSlug: "alice",
    }),
    /declared operator public key/,
  );

  await assert.rejects(
    runtime.createAgent({
      name: "Oversized Web Signed Assistant",
      provider: "codex",
      coreIdentity: {
        operatorSlug: "alice",
        agentSlug: "alice-agent",
        identityType: "agent",
        declaredOperatorPublicKey: "k".repeat(4097),
        source: "web_signed",
      },
    }),
    /declared operator public key/,
  );
});

test("RuntimeManager rejects malformed native identity shape", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-identity-malformed-"));
  const store = new StateStore(root);
  await store.init();
  const runtime = new RuntimeManager(store, new LogStore(root), new MalformedIdentityCoreNative());

  await assert.rejects(
    runtime.createAgent({
      name: "Malformed Native Agent",
      provider: "codex",
      operatorSlug: "alice",
    }),
    /native core returned invalid agent identity/,
  );

  const agents = await store.listAgents();
  assert.equal(agents.length, 1);
  assert.equal(agents[0]?.status, "error");
  assert.match(agents[0]?.lastError ?? "", /native core returned invalid agent identity/);
});

test("RuntimeManager rejects supplied identities without a declared operator public key", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-web-signed-missing-operator-key-"));
  const store = new StateStore(root);
  await store.init();
  const runtime = new RuntimeManager(store, new LogStore(root), new FakeCoreNative());

  await assert.rejects(
    runtime.createAgent({
      name: "Incomplete Web Signed Assistant",
      provider: "codex",
      coreIdentity: {
        operatorSlug: "alice",
        agentSlug: "alice-agent",
        identityType: "agent",
        source: "web_signed",
      } as any,
    }),
    /declared operator public key/,
  );

  assert.deepEqual(await store.listAgents(), []);
});

test("RuntimeManager rejects malformed supplied core identity shape", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-web-signed-malformed-"));
  const store = new StateStore(root);
  await store.init();
  const runtime = new RuntimeManager(store, new LogStore(root), new FakeCoreNative());

  await assert.rejects(
    runtime.createAgent({
      name: "Malformed Web Signed Assistant",
      provider: "codex",
      coreIdentity: {
        operatorSlug: "Alice",
        agentSlug: "bad slug",
        identityType: "agent",
        declaredOperatorPublicKey: "operator-pub",
      } as any,
    }),
    /operatorSlug and agentSlug/,
  );
});

test("RuntimeManager rejects supplied identities without a declared operator public key during preview", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-web-signed-preview-missing-operator-key-"));
  const store = new StateStore(root);
  await store.init();
  const runtime = new RuntimeManager(store, new LogStore(root), new FakeCoreNative());

  await assert.rejects(
    runtime.previewCreateAgent({
      name: "Incomplete Preview Web Signed Assistant",
      provider: "codex",
      coreIdentity: {
        operatorSlug: "alice",
        agentSlug: "alice-agent",
        identityType: "agent",
        source: "web_signed",
      } as any,
    }),
    /declared operator public key/,
  );
});

test("RuntimeManager accepts a Web-signed core identity without native identity creation", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-web-signed-"));
  const store = new StateStore(root);
  await store.init();
  const runtime = new RuntimeManager(store, new LogStore(root), new FailingIdentityCoreNative());

  const agent = await runtime.createAgent({
    name: "Web Signed Assistant",
    provider: "codex",
    coreIdentity: {
      operatorSlug: "alice",
      agentSlug: "alice-agent",
      identityType: "agent",
      declaredOperatorPublicKey: "operator-pub",
      source: "web_signed",
    },
  });

  assert.equal(agent.status, "created");
  assert.equal(agent.coreIdentity?.operatorSlug, "alice");
  assert.equal(agent.coreIdentity?.agentSlug, "alice-agent");
  assert.equal(agent.coreIdentity?.source, "web_signed");

  const saved = await store.getAgent(agent.id);
  assert.equal(saved?.coreIdentity?.declaredOperatorPublicKey, "operator-pub");
});

test("RuntimeManager strips unsupported supplied core identity fields during preview", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-web-signed-preview-strip-"));
  const store = new StateStore(root);
  await store.init();
  const runtime = new RuntimeManager(store, new LogStore(root), new FailingIdentityCoreNative());

  const preview = await runtime.previewCreateAgent({
    name: "Preview Strip Assistant",
    provider: "codex",
    coreIdentity: {
      operatorSlug: "alice",
      agentSlug: "alice-agent",
      identityType: "agent",
      declaredOperatorPublicKey: "operator-pub",
      source: "web_signed",
      root_secret_key: "must-not-enter-preview",
    } as any,
  });

  assert.equal((preview.agent.coreIdentity as any)?.root_secret_key, undefined);
  assert.equal(preview.agent.coreIdentity?.declaredOperatorPublicKey, "operator-pub");
});

test("StateStore strips unsupported core identity fields when saving agents", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-state-core-identity-save-strip-"));
  const store = new StateStore(root);
  await store.init();
  const agent = await store.createAgent({
    name: "Save Strip Assistant",
    provider: "codex",
    coreIdentity: {
      operatorSlug: "alice",
      agentSlug: "alice-agent",
      identityType: "agent",
      declaredOperatorPublicKey: "operator-pub",
      source: "web_signed",
    },
  });
  (agent.coreIdentity as any).root_secret_key = "must-not-enter-state";

  await store.saveAgent(agent);

  const raw = await fs.readFile(path.join(root, "agents", agent.id, "agent.json"), "utf8");
  assert.doesNotMatch(raw, /must-not-enter-state/);
  assert.equal((JSON.parse(raw).coreIdentity as any).root_secret_key, undefined);
});

test("RuntimeManager defaults and coerces supplied core identity source to web_signed", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-web-signed-default-source-"));
  const store = new StateStore(root);
  await store.init();
  const runtime = new RuntimeManager(store, new LogStore(root), new FailingIdentityCoreNative());

  const agent = await runtime.createAgent({
    name: "Default Source Assistant",
    provider: "codex",
    coreIdentity: {
      operatorSlug: "alice",
      agentSlug: "alice-agent",
      identityType: "agent",
      declaredOperatorPublicKey: "operator-pub",
    },
  });

  assert.equal(agent.coreIdentity?.source, "web_signed");
  const saved = await store.getAgent(agent.id);
  assert.equal(saved?.coreIdentity?.source, "web_signed");

  const coerced = await runtime.createAgent({
    name: "Coerced Source Assistant",
    provider: "codex",
    coreIdentity: {
      operatorSlug: "alice",
      agentSlug: "alice-agent-2",
      identityType: "agent",
      declaredOperatorPublicKey: "operator-pub",
      source: "native",
    },
  });
  assert.equal(coerced.coreIdentity?.source, "web_signed");
});

test("RuntimeManager does not leave Web-signed agents running when native session open fails", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-web-signed-start-fail-"));
  const store = new StateStore(root);
  await store.init();
  const provider = new FakeProviderSession();
  const runtime = new RuntimeManager(store, new LogStore(root), new FailingSessionCoreNative(), {
    providerFactory: () => provider,
  });

  await assert.rejects(
    runtime.createAgent({
      name: "Web Signed Assistant",
      provider: "codex",
      coreIdentity: {
        operatorSlug: "alice",
        agentSlug: "alice-agent",
        identityType: "agent",
        declaredOperatorPublicKey: "operator-pub",
        source: "web_signed",
      },
      start: true,
    }),
    /open session failed/,
  );

  const agents = await store.listAgents();
  assert.equal(agents.length, 1);
  assert.equal(agents[0]?.status, "error");
  assert.match(agents[0]?.lastError ?? "", /open session failed/);
  assert.equal(provider.getStatus().state, "stopped");
});

test("RuntimeManager generates bounded default agent slugs for core identity", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-identity-slug-"));
  const store = new StateStore(root);
  await store.init();
  const core = new FakeCoreNative();
  const runtime = new RuntimeManager(store, new LogStore(root), core);

  const agent = await runtime.createAgent({
    name: "Sam Assistant ".repeat(20),
    provider: "codex",
    operatorSlug: "alice",
  });

  assert.equal(agent.coreIdentity?.operatorSlug, "alice");
  assert.match(agent.coreIdentity?.agentSlug ?? "", /^[a-z0-9][a-z0-9-]{0,62}$/);
  assert.ok((agent.coreIdentity?.agentSlug.length ?? 0) <= 63);
  assert.match(agent.coreIdentity?.agentSlug ?? "", /^sam-assistant/);
});

test("RuntimeManager persists identity creation failures on the agent", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-identity-fail-"));
  const store = new StateStore(root);
  await store.init();
  const runtime = new RuntimeManager(store, new LogStore(root), new FailingIdentityCoreNative());

  await assert.rejects(
    runtime.createAgent({
      name: "Broken Identity",
      provider: "codex",
      operatorSlug: "alice",
    }),
    /identity create failed/,
  );

  const agents = await store.listAgents();
  assert.equal(agents.length, 1);
  assert.equal(agents[0]?.status, "error");
  assert.match(agents[0]?.lastError ?? "", /identity create failed/);
});

test("RuntimeManager ticks core message loop and sends provider replies", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-loop-"));
  const store = new StateStore(root);
  await store.init();
  const core = new FakeCoreNative();
  const provider = new FakeProviderSession();
  const runtime = new RuntimeManager(store, new LogStore(root), core, {
    autoStartMessageLoop: false,
    providerFactory: () => provider,
  });

  const agent = await runtime.createAgent({
    name: "Sam Assistant",
    provider: "codex",
    operatorSlug: "alice",
  });
  core.pending.push(
    {
      id: "self-message",
      body: "ignore me",
      senderSlug: "sam-assistant",
      mentioned: true,
      dm: false,
      mustRespond: true,
    },
    {
      id: "message-1",
      body: "status?",
      senderSlug: "alice",
      spaceId: "space-1",
      channelId: "channel-1",
      mentioned: true,
      dm: false,
      mustRespond: true,
    },
  );

  await runtime.startAgent(agent.id);
  const handled = await runtime.tickAgent(agent.id);
  const status = await runtime.getAgentStatus(agent.id);

  assert.equal(handled, 1);
  assert.equal(status.runtime.attached, true);
  assert.equal(status.runtime.coreSessionOpen, true);
  assert.equal(status.runtime.messageLoopOpen, true);
  assert.equal(status.runtime.providerStatus?.state, "ready");
  assert.equal(provider.inputs.length, 1);
  assert.equal(provider.inputs[0]?.body, "status?");
  assert.equal(core.channelReplies.length, 1);
  assert.equal(core.channelReplies[0]?.body, "ack: status?");
});

test("RuntimeManager sends a visible fallback when a must-respond message is silent", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-loop-silent-"));
  const store = new StateStore(root);
  await store.init();
  const core = new FakeCoreNative();
  const runtime = new RuntimeManager(store, new LogStore(root), core, {
    autoStartMessageLoop: false,
    providerFactory: () => new SilentProviderSession(),
  });

  const agent = await runtime.createAgent({
    name: "Sam Assistant",
    provider: "codex",
    operatorSlug: "alice",
  });
  core.pending.push({
    id: "message-1",
    body: "@sam-assistant status?",
    senderSlug: "alice",
    spaceId: "space-1",
    channelId: "channel-1",
    mentioned: true,
    dm: false,
    mustRespond: true,
  });

  await runtime.startAgent(agent.id);
  const handled = await runtime.tickAgent(agent.id);

  assert.equal(handled, 1);
  assert.equal(core.channelReplies.length, 1);
  assert.match(String(core.channelReplies[0]?.body ?? ""), /did not produce a response/);
});

test("RuntimeManager converts provider exceptions into visible message errors", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-loop-throw-"));
  const store = new StateStore(root);
  await store.init();
  const core = new FakeCoreNative();
  const runtime = new RuntimeManager(store, new LogStore(root), core, {
    autoStartMessageLoop: false,
    providerFactory: () => new ThrowingProviderSession(),
  });

  const agent = await runtime.createAgent({
    name: "Sam Assistant",
    provider: "codex",
    operatorSlug: "alice",
  });
  core.pending.push({
    id: "message-1",
    body: "@sam-assistant status?",
    senderSlug: "alice",
    spaceId: "space-1",
    channelId: "channel-1",
    mentioned: true,
    dm: false,
    mustRespond: true,
  });

  await runtime.startAgent(agent.id);
  const handled = await runtime.tickAgent(agent.id);

  assert.equal(handled, 1);
  assert.equal(core.channelReplies.length, 1);
  assert.match(String(core.channelReplies[0]?.body ?? ""), /Agent error: provider crashed/);
});

test("RuntimeManager redacts visible provider runtime errors", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-loop-redact-"));
  const store = new StateStore(root);
  await store.init();
  const core = new FakeCoreNative();
  const runtime = new RuntimeManager(store, new LogStore(root), core, {
    autoStartMessageLoop: false,
    providerFactory: () => new SecretThrowingProviderSession(),
  });

  const agent = await runtime.createAgent({
    name: "Sam Assistant",
    provider: "codex",
    operatorSlug: "alice",
  });
  core.pending.push({
    id: "message-1",
    body: "@sam-assistant status?",
    senderSlug: "alice",
    spaceId: "space-1",
    channelId: "channel-1",
    mentioned: true,
    dm: false,
    mustRespond: true,
  });

  await runtime.startAgent(agent.id);
  await runtime.tickAgent(agent.id);

  const saved = await store.getAgent(agent.id);
  assert.equal(saved?.lastError, undefined);
  assert.doesNotMatch(String(core.channelReplies[0]?.body ?? ""), /secret-value/);
  assert.match(String(core.channelReplies[0]?.body ?? ""), /token=\[redacted\]/);
});

test("RuntimeManager redacts persisted start errors", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-start-redact-"));
  const store = new StateStore(root);
  await store.init();
  const runtime = new RuntimeManager(store, new LogStore(root), undefined, {
    providerFactory: () => new SecretStartFailProviderSession(),
  });

  const agent = await runtime.createAgent({
    name: "Broken Agent",
    provider: "codex",
  });
  await assert.rejects(runtime.startAgent(agent.id), /token=\[redacted\]/);

  const saved = await store.getAgent(agent.id);
  assert.equal(saved?.status, "error");
  assert.doesNotMatch(saved?.lastError ?? "", /secret-value/);
  assert.match(saved?.lastError ?? "", /token=\[redacted\]/);
});

class FakeCoreNative implements CoreNative {
  readonly pending: OpenedAgentMessage[] = [];
  readonly channelReplies: Array<Record<string, unknown>> = [];

  async openOrCreateDevice() {
    return { connected: true, status: "ready" as const };
  }

  async startPairing() {
    return { status: "unavailable" as const };
  }

  async confirmPairing() {
    return { connected: true, status: "ready" as const };
  }

  async createAgentIdentity(input: { operatorSlug: string; agentSlug: string }) {
    return {
      ok: true,
      operatorSlug: input.operatorSlug,
      agentSlug: input.agentSlug,
      identityType: "agent" as const,
      declaredOperatorPublicKey: "operator-pub",
    };
  }

  async openAgentSession(input: Record<string, unknown>) {
    return `handle:${String(input.agentSlug ?? "unknown")}`;
  }

  async syncOnce() {
    return {};
  }

  async processPendingMessages() {
    return this.pending.splice(0);
  }

  async sendChannelReply(_handle: string, input: Record<string, unknown>) {
    this.channelReplies.push(input);
    return { messageId: "message" };
  }

  async sendDirectReply() {
    return { messageId: "message" };
  }

  async snapshot() {
    return {};
  }

  async closeSession() {}
}

class FailingIdentityCoreNative extends FakeCoreNative {
  override async createAgentIdentity(_input: { operatorSlug: string; agentSlug: string }): Promise<never> {
    throw new Error("identity create failed");
  }
}

class MissingOperatorKeyCoreNative extends FakeCoreNative {
  override async createAgentIdentity(input: { operatorSlug: string; agentSlug: string }): Promise<any> {
    return {
      ok: true,
      operatorSlug: input.operatorSlug,
      agentSlug: input.agentSlug,
      identityType: "agent" as const,
    };
  }
}

class OversizedOperatorKeyCoreNative extends FakeCoreNative {
  override async createAgentIdentity(input: { operatorSlug: string; agentSlug: string }): Promise<any> {
    return {
      ok: true,
      operatorSlug: input.operatorSlug,
      agentSlug: input.agentSlug,
      identityType: "agent" as const,
      declaredOperatorPublicKey: "k".repeat(4097),
    };
  }
}

class MalformedIdentityCoreNative extends FakeCoreNative {
  override async createAgentIdentity(input: { operatorSlug: string; agentSlug: string }): Promise<any> {
    return {
      ok: true,
      operatorSlug: input.operatorSlug,
      agentSlug: "bad slug",
      identityType: "agent" as const,
      declaredOperatorPublicKey: "operator-pub",
    };
  }
}

class FailingSessionCoreNative extends FakeCoreNative {
  override async openAgentSession(): Promise<never> {
    throw new Error("open session failed");
  }
}

class ThrowingSyncCoreNative extends FakeCoreNative {
  override async syncOnce(): Promise<never> {
    throw new Error("sync failed");
  }
}

class CountingProviderSession implements ProviderSession {
  started = false;
  stopped = false;

  async start() {
    this.started = true;
  }

  async stop() {
    this.stopped = true;
  }

  async send(_input: AgentInput): Promise<AgentOutput> {
    return { kind: "silent" };
  }

  async resetSession() {}

  getStatus(): ProviderStatus {
    return { state: this.stopped ? "stopped" : this.started ? "ready" : "idle" };
  }
}

class SessionReportingProviderSession implements ProviderSession {
  private status: ProviderStatus = { state: "idle" };

  constructor(private readonly sessionId: string) {}

  async start() {
    this.status = { state: "ready" };
  }

  async stop() {
    this.status = { state: "stopped", sessionId: this.sessionId };
  }

  async send(_input: AgentInput): Promise<AgentOutput> {
    this.status = { state: "ready", sessionId: this.sessionId };
    return { kind: "reply", body: "ack" };
  }

  async resetSession() {
    this.status = { state: "ready" };
  }

  getStatus(): ProviderStatus {
    return this.status;
  }
}

class ThrowingProviderSession implements ProviderSession {
  async start() {}

  async stop() {}

  async send(_input: AgentInput): Promise<AgentOutput> {
    throw new Error("provider crashed");
  }

  async resetSession() {}

  getStatus(): ProviderStatus {
    return { state: "ready" };
  }
}

class SilentProviderSession implements ProviderSession {
  async start() {}

  async stop() {}

  async send(_input: AgentInput): Promise<AgentOutput> {
    return { kind: "silent" };
  }

  async resetSession() {}

  getStatus(): ProviderStatus {
    return { state: "ready" };
  }
}

class SecretThrowingProviderSession implements ProviderSession {
  async start() {}

  async stop() {}

  async send(_input: AgentInput): Promise<AgentOutput> {
    throw new Error("provider crashed token=secret-value");
  }

  async resetSession() {}

  getStatus(): ProviderStatus {
    return { state: "ready" };
  }
}

class SecretStartFailProviderSession implements ProviderSession {
  async start() {
    throw new Error("provider start failed token=secret-value");
  }

  async stop() {}

  async send(_input: AgentInput): Promise<AgentOutput> {
    return { kind: "silent" };
  }

  async resetSession() {}

  getStatus(): ProviderStatus {
    return { state: "error", lastError: "provider start failed token=secret-value" };
  }
}

class SecretStopFailProviderSession implements ProviderSession {
  async start() {}

  async stop() {
    throw new Error("provider stop failed token=secret-value");
  }

  async send(_input: AgentInput): Promise<AgentOutput> {
    return { kind: "silent" };
  }

  async resetSession() {}

  getStatus(): ProviderStatus {
    return { state: "ready" };
  }
}

class SecretStatusProviderSession implements ProviderSession {
  async start() {}

  async stop() {}

  async send(_input: AgentInput): Promise<AgentOutput> {
    return { kind: "silent" };
  }

  async resetSession() {}

  getStatus(): ProviderStatus {
    return { state: "error", lastError: "provider status failed token=secret-value" };
  }
}

class FakeProviderSession implements ProviderSession {
  readonly inputs: AgentInput[] = [];
  private status: ProviderStatus = { state: "idle" };

  async start() {
    this.status = { state: "ready" };
  }

  async stop() {
    this.status = { state: "stopped" };
  }

  async send(input: AgentInput): Promise<AgentOutput> {
    this.inputs.push(input);
    return { kind: "reply", body: `ack: ${input.body}` };
  }

  async resetSession() {
    this.status = { state: "ready" };
  }

  getStatus(): ProviderStatus {
    return this.status;
  }
}
