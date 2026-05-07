import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { LogStore } from "../src/logs/log-store.js";
import { SidecarCoreNative } from "../src/native/sidecar-core.js";
import { ProviderSession } from "../src/providers/provider-types.js";
import { RuntimeManager } from "../src/runtime/runtime-manager.js";
import { StateStore } from "../src/state/store.js";
import { AgentInput, AgentOutput, ProviderStatus } from "../src/types.js";

test("RuntimeManager handles a sidecar-opened message and sends a sidecar reply", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-runtime-sidecar-"));
  const store = new StateStore(root);
  await store.init();
  const core = new SidecarCoreNative();
  const provider = new FakeProviderSession();
  const runtime = new RuntimeManager(store, new LogStore(root), core, {
    autoStartMessageLoop: false,
    providerFactory: () => provider,
  });

  try {
    const agent = await runtime.createAgent({
      name: "Alice Agent",
      provider: "codex",
      operatorSlug: "alice",
    });
    await runtime.startAgent(agent.id);

    assert(agent.coreIdentity);
    await core.devInjectChannelMessage({
      senderSlug: "alice",
      agentSlug: agent.coreIdentity.agentSlug,
      body: `@${agent.coreIdentity.agentSlug} ping`,
    });

    const handled = await runtime.tickAgent(agent.id);
    assert.equal(handled, 1);
    assert.equal(provider.inputs.length, 1);
    assert.equal(provider.inputs[0]?.mustRespond, true);
    assert.equal(provider.inputs[0]?.senderSlug, "alice");
    assert.match(provider.inputs[0]?.body ?? "", /ping/);

    await core.devInjectChannelMessage({
      senderSlug: "alice",
      agentSlug: agent.coreIdentity.agentSlug,
      body: `@${agent.coreIdentity.agentSlug}-extra ambient update`,
    });

    const ambientHandled = await runtime.tickAgent(agent.id);
    assert.equal(ambientHandled, 1);
    assert.equal(provider.inputs.length, 2);
    assert.equal(provider.inputs[1]?.mentioned, false);
    assert.equal(provider.inputs[1]?.mustRespond, false);

    const updated = await store.getAgent(agent.id);
    assert.equal(updated?.status, "running");
    assert.match(updated?.lastActiveAt ?? "", /^\d{4}-/);
  } finally {
    const agents = await runtime.listAgents();
    await Promise.all(agents.map((agent) => runtime.stopAgent(agent.id)));
    await core.shutdown();
  }
});

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
