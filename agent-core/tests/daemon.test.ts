import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { DaemonSupervisor } from "../src/daemon/supervisor.js";
import { createCoreNative, startDaemon } from "../src/daemon/daemon.js";
import { LogStore } from "../src/logs/log-store.js";
import type { DeviceStatus } from "../src/native/core.js";
import { ProviderSession } from "../src/providers/provider-types.js";
import { RuntimeManager } from "../src/runtime/runtime-manager.js";
import { StateStore } from "../src/state/store.js";
import { AgentInput, AgentOutput, ProviderStatus } from "../src/types.js";

test("startDaemon resumes agents persisted as running", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-daemon-resume-"));
  const store = new StateStore(root);
  await store.init();
  const agent = await store.createAgent({
    name: "Resume Agent",
    provider: "codex",
  });
  agent.status = "running";
  agent.coreIdentity = {
    operatorSlug: "alice",
    agentSlug: "resume-agent",
    identityType: "agent",
    declaredOperatorPublicKey: "operator-pub",
  };
  await store.saveAgent(agent);

  const daemon = await startDaemon({ port: 0, stateHome: root });
  try {
    assert.notEqual(daemon.port, 0);
    const daemonState = await store.readDaemon();
    assert.equal(daemonState?.port, daemon.port);
    const resumed = await daemon.store.getAgent(agent.id);
    assert.equal(resumed?.status, "running");
  } finally {
    await daemon.close();
  }

  const stopped = await store.getAgent(agent.id);
  assert.equal(stopped?.status, "stopped");
});

test("DaemonSupervisor does not resume legacy running agents without core identity", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-daemon-resume-no-identity-"));
  const store = new StateStore(root);
  await store.init();
  const runtime = new RuntimeManager(store, new LogStore(root), undefined, {
    providerFactory: () => new FailingProviderSession(),
  });
  const agent = await store.createAgent({
    name: "Legacy Running Agent",
    provider: "codex",
  });
  agent.status = "running";
  await store.saveAgent(agent);

  const report = await new DaemonSupervisor(runtime).startPersistedRunningAgents();

  assert.equal(report.attempted, 1);
  assert.equal(report.started, 0);
  assert.equal(report.failed.length, 1);
  assert.equal(report.failed[0]?.id, agent.id);
  assert.match(report.failed[0]?.error ?? "", /coreIdentity/);
  const saved = await store.getAgent(agent.id);
  assert.equal(saved?.status, "error");
  assert.match(saved?.lastError ?? "", /coreIdentity/);
});

test("createCoreNative passes daemon stateHome to sidecar defaults", { skip: process.platform === "win32" }, async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-daemon-core-env-"));
  const script = path.join(root, "env-sidecar.js");
  await fs.writeFile(
    script,
    [
      "#!/usr/bin/env node",
      "process.stdin.setEncoding('utf8');",
      "let buffer = '';",
      "process.stdin.on('data', (chunk) => {",
      "  buffer += chunk;",
      "  let index;",
      "  while ((index = buffer.indexOf('\\n')) >= 0) {",
      "    const line = buffer.slice(0, index).trim();",
      "    buffer = buffer.slice(index + 1);",
      "    if (!line) continue;",
      "    const request = JSON.parse(line);",
      "    process.stdout.write(JSON.stringify({",
      "      id: request.id,",
      "      ok: true,",
      "      result: {",
      "        connected: true,",
      "        status: 'ready',",
      "        env: {",
      "          db: process.env.AGENT_CORE_DATABASE_PATH ?? null,",
      "          server: process.env.AGENT_CORE_SERVER_URL ?? null",
      "        }",
      "      }",
      "    }) + '\\n');",
      "  }",
      "});",
    ].join("\n"),
    { mode: 0o700 },
  );
  const previousNative = process.env.AGENT_CORE_NATIVE;
  const previousSidecarBin = process.env.AGENT_CORE_SIDECAR_BIN;
  const previousDbPath = process.env.AGENT_CORE_DATABASE_PATH;
  const previousServer = process.env.AGENT_CORE_SERVER_URL;
  delete process.env.AGENT_CORE_NATIVE;
  process.env.AGENT_CORE_SIDECAR_BIN = script;
  delete process.env.AGENT_CORE_DATABASE_PATH;
  delete process.env.AGENT_CORE_SERVER_URL;
  const core = createCoreNative({ stateHome: root });

  try {
    const device = await core.openOrCreateDevice({}) as DeviceStatus & {
      env: Record<string, string | null>;
    };
    assert.equal(device.env.db, path.join(root, "core.sqlite"));
    assert.equal(device.env.server, "https://api.puffo.ai");
  } finally {
    await core.shutdown?.();
    await fs.rm(root, { recursive: true, force: true });
    restoreEnv("AGENT_CORE_NATIVE", previousNative);
    restoreEnv("AGENT_CORE_SIDECAR_BIN", previousSidecarBin);
    restoreEnv("AGENT_CORE_DATABASE_PATH", previousDbPath);
    restoreEnv("AGENT_CORE_SERVER_URL", previousServer);
  }
});

test("DaemonSupervisor reports failed agent resume without throwing", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-daemon-resume-fail-"));
  const store = new StateStore(root);
  await store.init();
  const runtime = new RuntimeManager(store, new LogStore(root), undefined, {
    providerFactory: () => new FailingProviderSession(),
  });
  const agent = await store.createAgent({
    name: "Broken Agent",
    provider: "codex",
  });
  agent.status = "running";
  agent.coreIdentity = {
    operatorSlug: "alice",
    agentSlug: "broken-agent",
    identityType: "agent",
    declaredOperatorPublicKey: "operator-pub",
  };
  await store.saveAgent(agent);

  const report = await new DaemonSupervisor(runtime).startPersistedRunningAgents();

  assert.equal(report.attempted, 1);
  assert.equal(report.started, 0);
  assert.equal(report.failed.length, 1);
  assert.equal(report.failed[0]?.id, agent.id);
  const saved = await store.getAgent(agent.id);
  assert.equal(saved?.status, "error");
  assert.match(saved?.lastError ?? "", /provider start failed/);
});

test("DaemonSupervisor stopAll is best-effort across running agents", async () => {
  const calls: string[] = [];
  const runtime = {
    async listAgents() {
      return [
        { id: "agent-a", status: "running" },
        { id: "agent-b", status: "running" },
        { id: "agent-c", status: "stopped" },
      ];
    },
    async stopAgent(id: string) {
      calls.push(id);
      if (id === "agent-a") throw new Error("stop failed");
      return { id, status: "stopped" };
    },
  } as unknown as RuntimeManager;

  await new DaemonSupervisor(runtime).stopAll();

  assert.deepEqual(calls.sort(), ["agent-a", "agent-b"]);
});

test("startDaemon ignores stale daemon files when the pid is alive but health does not match", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-daemon-stale-"));
  const store = new StateStore(root);
  await store.init();
  await store.saveDaemon({
    pid: process.pid,
    host: "127.0.0.1",
    port: 9,
    startedAt: new Date().toISOString(),
  });

  const daemon = await startDaemon({ port: 0, stateHome: root });
  try {
    assert.notEqual(daemon.port, 0);
    const state = await store.readDaemon();
    assert.equal(state?.pid, process.pid);
    assert.equal(state?.port, daemon.port);
  } finally {
    await daemon.close();
  }
});

class FailingProviderSession implements ProviderSession {
  async start() {
    throw new Error("provider start failed");
  }

  async stop() {}

  async send(_input: AgentInput): Promise<AgentOutput> {
    return { kind: "silent" };
  }

  async resetSession() {}

  getStatus(): ProviderStatus {
    return { state: "error", lastError: "provider start failed" };
  }
}

function restoreEnv(key: string, value: string | undefined): void {
  if (value === undefined) delete process.env[key];
  else process.env[key] = value;
}
