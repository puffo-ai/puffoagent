import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { runStop } from "../src/cli/stop.js";
import { StateStore } from "../src/state/store.js";

test("runStop removes a stale daemon file without killing a reused pid", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-cli-stop-"));
  const previousHome = process.env.AGENT_CORE_HOME;
  process.env.AGENT_CORE_HOME = root;
  const store = new StateStore(root);
  await store.init();
  await store.saveDaemon({
    pid: process.pid,
    host: "127.0.0.1",
    port: 9,
    startedAt: new Date().toISOString(),
  });

  const originalLog = console.log;
  const lines: string[] = [];
  console.log = (...args: unknown[]) => {
    lines.push(args.map(String).join(" "));
  };
  try {
    await runStop();
  } finally {
    console.log = originalLog;
    if (previousHome === undefined) delete process.env.AGENT_CORE_HOME;
    else process.env.AGENT_CORE_HOME = previousHome;
  }

  assert.equal(lines[0], "removed stale daemon pid file");
  assert.equal(await store.readDaemon(), undefined);
});

test("runStop removes a corrupt daemon file", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-cli-stop-corrupt-"));
  const previousHome = process.env.AGENT_CORE_HOME;
  process.env.AGENT_CORE_HOME = root;
  const store = new StateStore(root);
  await store.init();
  await fs.writeFile(path.join(root, "daemon.json"), "{bad-json");

  const originalLog = console.log;
  const lines: string[] = [];
  console.log = (...args: unknown[]) => {
    lines.push(args.map(String).join(" "));
  };
  try {
    await runStop();
  } finally {
    console.log = originalLog;
    if (previousHome === undefined) delete process.env.AGENT_CORE_HOME;
    else process.env.AGENT_CORE_HOME = previousHome;
  }

  assert.equal(lines[0], "agent daemon is not running");
  await assert.rejects(fs.access(path.join(root, "daemon.json")), /ENOENT/);
});
