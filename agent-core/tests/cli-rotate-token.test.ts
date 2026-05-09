import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { runRotateToken } from "../src/cli/rotate-token.js";
import { startDaemon } from "../src/daemon/daemon.js";
import { StateStore } from "../src/state/store.js";

test("runRotateToken rotates local state when the daemon is stopped", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-cli-rotate-offline-"));
  const previousHome = process.env.AGENT_CORE_HOME;
  process.env.AGENT_CORE_HOME = root;
  const store = new StateStore(root);
  await store.init();
  const before = await store.ensureDeviceState();
  const grant = await store.createLocalAccessGrant({ ttlMs: 60_000 });
  const lines = await captureLogs(() => runRotateToken());

  try {
    const after = await store.ensureDeviceState();
    assert.notEqual(after.apiToken, before.apiToken);
    assert.equal(lines[0], "local control token rotated");
    assert.equal(lines[1], `local control token: ${after.apiToken}`);
    assert.equal(lines[2], "Existing local grants were revoked.");
    assert.equal(await store.verifyLocalAccessGrant(grant.token), false);
  } finally {
    if (previousHome === undefined) delete process.env.AGENT_CORE_HOME;
    else process.env.AGENT_CORE_HOME = previousHome;
  }
});

test("runRotateToken rotates through the running daemon API", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-cli-rotate-daemon-"));
  const previousHome = process.env.AGENT_CORE_HOME;
  const previousNative = process.env.AGENT_CORE_NATIVE;
  process.env.AGENT_CORE_HOME = root;
  process.env.AGENT_CORE_NATIVE = "unavailable";
  const daemon = await startDaemon({ stateHome: root, port: 0 });
  const before = await daemon.store.ensureDeviceState();
  const grant = await daemon.store.createLocalAccessGrant({ ttlMs: 60_000 });

  try {
    const lines = await captureLogs(() => runRotateToken());
    const token = lines[1]?.replace(/^local control token: /, "");
    assert.match(token ?? "", /^[A-Za-z0-9_-]+$/);
    assert.notEqual(token, before.apiToken);

    const oldTokenResponse = await fetch(`http://${daemon.host}:${daemon.port}/agents`, {
      headers: { Authorization: `Bearer ${before.apiToken}` },
    });
    assert.equal(oldTokenResponse.status, 401);

    const oldGrantResponse = await fetch(`http://${daemon.host}:${daemon.port}/agents`, {
      headers: { Authorization: `Bearer ${grant.token}` },
    });
    assert.equal(oldGrantResponse.status, 401);

    const newTokenResponse = await fetch(`http://${daemon.host}:${daemon.port}/agents`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    assert.equal(newTokenResponse.status, 200);
  } finally {
    await daemon.close();
    if (previousHome === undefined) delete process.env.AGENT_CORE_HOME;
    else process.env.AGENT_CORE_HOME = previousHome;
    if (previousNative === undefined) delete process.env.AGENT_CORE_NATIVE;
    else process.env.AGENT_CORE_NATIVE = previousNative;
  }
});

async function captureLogs(fn: () => Promise<void>): Promise<string[]> {
  const originalLog = console.log;
  const lines: string[] = [];
  console.log = (...args: unknown[]) => {
    lines.push(args.map(String).join(" "));
  };
  try {
    await fn();
    return lines;
  } finally {
    console.log = originalLog;
  }
}
