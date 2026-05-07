import assert from "node:assert/strict";
import { spawn, ChildProcessWithoutNullStreams } from "node:child_process";
import fs from "node:fs/promises";
import http from "node:http";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { runStart } from "../src/cli/start.js";
import { daemonIsRunning } from "../src/daemon/lockfile.js";
import { StateStore } from "../src/state/store.js";

test("runStart is idempotent when daemon pid file points at a running process", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-cli-start-"));
  const previousHome = process.env.AGENT_CORE_HOME;
  process.env.AGENT_CORE_HOME = root;
  const store = new StateStore(root);
  await store.init();
  const device = await store.ensureDeviceState();
  const healthServer = http.createServer((_req, res) => {
    res.setHeader("Content-Type", "application/json");
    res.end(JSON.stringify({ ok: true, stateHome: root }));
  });
  await new Promise<void>((resolve) => healthServer.listen(0, "127.0.0.1", resolve));
  const address = healthServer.address();
  assert(address && typeof address === "object");
  await store.saveDaemon({
    pid: process.pid,
    host: "127.0.0.1",
    port: address.port,
    startedAt: new Date().toISOString(),
  });

  const originalLog = console.log;
  const lines: string[] = [];
  console.log = (...args: unknown[]) => {
    lines.push(args.map(String).join(" "));
  };
  try {
    await runStart([]);
  } finally {
    console.log = originalLog;
    await new Promise<void>((resolve, reject) => healthServer.close((error) => (error ? reject(error) : resolve())));
    if (previousHome === undefined) delete process.env.AGENT_CORE_HOME;
    else process.env.AGENT_CORE_HOME = previousHome;
  }

  assert.match(lines[0] ?? "", /already listening/);
  assert.match(lines[0] ?? "", new RegExp(`http://127\\.0\\.0\\.1:${address.port}`));
  assert.equal(lines[1], `local control token: ${device.apiToken}`);
  assert.equal(lines[2], "Return to Web and click Re-check.");
});

test("runStart can print existing daemon connection info as JSON", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-cli-start-json-"));
  const previousHome = process.env.AGENT_CORE_HOME;
  process.env.AGENT_CORE_HOME = root;
  const store = new StateStore(root);
  await store.init();
  const device = await store.ensureDeviceState();
  const healthServer = http.createServer((_req, res) => {
    res.setHeader("Content-Type", "application/json");
    res.end(JSON.stringify({ ok: true, stateHome: root }));
  });
  await new Promise<void>((resolve) => healthServer.listen(0, "127.0.0.1", resolve));
  const address = healthServer.address();
  assert(address && typeof address === "object");
  await store.saveDaemon({
    pid: process.pid,
    host: "127.0.0.1",
    port: address.port,
    startedAt: new Date().toISOString(),
  });

  const originalLog = console.log;
  const lines: string[] = [];
  console.log = (...args: unknown[]) => {
    lines.push(args.map(String).join(" "));
  };
  try {
    await runStart(["--json"]);
  } finally {
    console.log = originalLog;
    await new Promise<void>((resolve, reject) => healthServer.close((error) => (error ? reject(error) : resolve())));
    if (previousHome === undefined) delete process.env.AGENT_CORE_HOME;
    else process.env.AGENT_CORE_HOME = previousHome;
  }

  assert.equal(lines.length, 1);
  const body = JSON.parse(lines[0] ?? "{}");
  assert.equal(body.status, "already_listening");
  assert.equal(body.ok, true);
  assert.equal(body.url, `http://127.0.0.1:${address.port}`);
  assert.equal(body.token, device.apiToken);
  assert.equal(body.authRequired, true);
});

test("daemon health can be verified by instance id when control token is corrupt", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-cli-start-instance-"));
  const store = new StateStore(root);
  await store.init();
  const instanceId = "instance-token-corrupt";
  const healthServer = http.createServer((_req, res) => {
    res.setHeader("Content-Type", "application/json");
    res.end(JSON.stringify({ ok: true, authRequired: true, instanceId }));
  });
  await new Promise<void>((resolve) => healthServer.listen(0, "127.0.0.1", resolve));
  const address = healthServer.address();
  assert(address && typeof address === "object");
  await store.saveDaemon({
    pid: process.pid,
    host: "127.0.0.1",
    port: address.port,
    startedAt: new Date().toISOString(),
    instanceId,
  });
  await fs.writeFile(path.join(root, "device.json"), "{bad-json");

  try {
    assert.equal(await daemonIsRunning(store), true);
  } finally {
    await new Promise<void>((resolve, reject) => healthServer.close((error) => (error ? reject(error) : resolve())));
  }
});

test("runStart does not print a regenerated token for an already-running daemon", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-cli-start-corrupt-device-"));
  const previousHome = process.env.AGENT_CORE_HOME;
  process.env.AGENT_CORE_HOME = root;
  const store = new StateStore(root);
  await store.init();
  const instanceId = "instance-running-corrupt-device";
  const healthServer = http.createServer((_req, res) => {
    res.setHeader("Content-Type", "application/json");
    res.end(JSON.stringify({ ok: true, authRequired: true, instanceId }));
  });
  await new Promise<void>((resolve) => healthServer.listen(0, "127.0.0.1", resolve));
  const address = healthServer.address();
  assert(address && typeof address === "object");
  await store.saveDaemon({
    pid: process.pid,
    host: "127.0.0.1",
    port: address.port,
    startedAt: new Date().toISOString(),
    instanceId,
  });
  await fs.writeFile(path.join(root, "device.json"), "{bad-json");

  try {
    await assert.rejects(runStart([]), /local control token is missing or corrupt/);
  } finally {
    await new Promise<void>((resolve, reject) => healthServer.close((error) => (error ? reject(error) : resolve())));
    if (previousHome === undefined) delete process.env.AGENT_CORE_HOME;
    else process.env.AGENT_CORE_HOME = previousHome;
  }
});

test("runStart rejects invalid explicit ports", async () => {
  await assert.rejects(runStart(["--port", "abc"]), /--port must be an integer port/);
  await assert.rejects(runStart(["--port", "0"]), /--port must be an integer port/);
  await assert.rejects(runStart(["--port"]), /--port requires a value/);
});

test("runStart rejects invalid AGENT_CORE_PORT", async () => {
  const previous = process.env.AGENT_CORE_PORT;
  process.env.AGENT_CORE_PORT = "70000";
  try {
    await assert.rejects(runStart([]), /AGENT_CORE_PORT must be an integer port/);
  } finally {
    if (previous === undefined) delete process.env.AGENT_CORE_PORT;
    else process.env.AGENT_CORE_PORT = previous;
  }
});

test("CLI start exposes health and stop terminates the daemon process", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-cli-e2e-"));
  const port = await availablePort();
  const cli = path.join(process.cwd(), "dist", "src", "cli", "index.js");
  const env = {
    ...process.env,
    AGENT_CORE_HOME: root,
    AGENT_CORE_PORT: String(port),
    AGENT_CORE_NATIVE: "unavailable",
  };
  const child = spawn(process.execPath, [cli, "start"], {
    env,
    windowsHide: true,
  });

  try {
    const output = await waitForOutput(child, /Return to Web and click Re-check\./, 5_000);
    assert.match(output, new RegExp(`http://127\\.0\\.0\\.1:${port}`));
    assert.match(output, /^agent daemon listening on http:\/\/127\.0\.0\.1:\d+$/m);
    assert.match(output, /^local control token: \S+$/m);
    assert.match(output, /^Return to Web and click Re-check\.$/m);

    const response = await fetch(`http://127.0.0.1:${port}/health`);
    assert.equal(response.status, 200);
    const health = (await response.json()) as { ok?: boolean; stateHome?: string; authRequired?: boolean };
    assert.equal(health.ok, true);
    assert.equal(health.stateHome, undefined);
    assert.equal(health.authRequired, true);

    await fs.writeFile(path.join(root, "device.json"), "{bad-json");
    const stop = spawn(process.execPath, [cli, "stop"], { env, windowsHide: true });
    const stopOutput = await waitForOutput(stop, /sent SIGTERM/, 5_000);
    assert.match(stopOutput, /sent SIGTERM/);
    await waitForExit(stop, 5_000);
    await waitForExit(child, 5_000);
  } finally {
    if (child.exitCode === null && child.signalCode === null) child.kill("SIGTERM");
    await fs.rm(root, { recursive: true, force: true });
  }
});

test("CLI start --json exposes machine-readable connection info", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-cli-json-e2e-"));
  const port = await availablePort();
  const cli = path.join(process.cwd(), "dist", "src", "cli", "index.js");
  const env = {
    ...process.env,
    AGENT_CORE_HOME: root,
    AGENT_CORE_PORT: String(port),
    AGENT_CORE_NATIVE: "unavailable",
  };
  const child = spawn(process.execPath, [cli, "start", "--json"], {
    env,
    windowsHide: true,
  });

  try {
    const output = await waitForOutput(child, /"status":"listening"/, 5_000);
    const jsonLine = output.split(/\r?\n/).find((line) => line.includes('"status":"listening"'));
    assert(jsonLine);
    const body = JSON.parse(jsonLine);
    assert.equal(body.status, "listening");
    assert.equal(body.ok, true);
    assert.equal(body.url, `http://127.0.0.1:${port}`);
    assert.equal(body.host, "127.0.0.1");
    assert.equal(body.port, port);
    assert.equal(body.authRequired, true);
    assert.match(body.token, /^[A-Za-z0-9_-]+$/);

    const response = await fetch(`${body.url}/health`);
    assert.equal(response.status, 200);

    const stop = spawn(process.execPath, [cli, "stop"], { env, windowsHide: true });
    await waitForOutput(stop, /sent SIGTERM/, 5_000);
    await waitForExit(stop, 5_000);
    await waitForExit(child, 5_000);
  } finally {
    if (child.exitCode === null && child.signalCode === null) child.kill("SIGTERM");
    await fs.rm(root, { recursive: true, force: true });
  }
});

test("built CLI bin is directly executable", async () => {
  const cli = path.join(process.cwd(), "dist", "src", "cli", "index.js");
  if (process.platform !== "win32") {
    assert.notEqual((await fs.stat(cli)).mode & 0o111, 0);
  }

  const child = spawn(cli, ["version"], { windowsHide: true });
  const output = await waitForOutput(child, /0\.1\.0/, 5_000);
  assert.match(output, /0\.1\.0/);
  await waitForExit(child, 5_000);
});

async function availablePort(): Promise<number> {
  const server = net.createServer();
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => resolve());
  });
  const address = server.address();
  assert(address && typeof address === "object");
  const port = address.port;
  await new Promise<void>((resolve, reject) => server.close((error) => (error ? reject(error) : resolve())));
  return port;
}

function waitForOutput(
  child: ChildProcessWithoutNullStreams,
  pattern: RegExp,
  timeoutMs: number,
): Promise<string> {
  return new Promise((resolve, reject) => {
    let output = "";
    const timer = setTimeout(() => {
      cleanup();
      reject(new Error(`timed out waiting for ${pattern}: ${output}`));
    }, timeoutMs);
    const onData = (chunk: Buffer) => {
      output += chunk.toString("utf8");
      if (pattern.test(output)) {
        cleanup();
        resolve(output);
      }
    };
    const onExit = (code: number | null, signal: NodeJS.Signals | null) => {
      cleanup();
      reject(new Error(`process exited before output matched ${pattern}: code=${code} signal=${signal} output=${output}`));
    };
    const cleanup = () => {
      clearTimeout(timer);
      child.stdout.off("data", onData);
      child.stderr.off("data", onData);
      child.off("exit", onExit);
    };
    child.stdout.on("data", onData);
    child.stderr.on("data", onData);
    child.once("exit", onExit);
  });
}

function waitForExit(child: ChildProcessWithoutNullStreams, timeoutMs: number): Promise<void> {
  if (child.exitCode !== null || child.signalCode !== null) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      cleanup();
      reject(new Error("timed out waiting for process exit"));
    }, timeoutMs);
    const onExit = () => {
      cleanup();
      resolve();
    };
    const cleanup = () => {
      clearTimeout(timer);
      child.off("exit", onExit);
    };
    child.once("exit", onExit);
  });
}
