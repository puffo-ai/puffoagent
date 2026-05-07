import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { LogStore } from "../src/logs/log-store.js";

test("LogStore redacts secrets and writes private log files", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-logs-"));
  const logs = new LogStore(root);

  await logs.append(
    "agent-1",
    'authorization: bearer abc token=secret sk-testsecret123456 Keychain account=user@example.com {"authToken":"json-secret","apiKey":"json-api-key"}',
  );

  const lines = await logs.tail("agent-1");
  assert.equal(lines.length, 1);
  assert.match(lines[0] ?? "", /authorization: bearer \[redacted\]/i);
  assert.match(lines[0] ?? "", /token=\[redacted\]/);
  assert.match(lines[0] ?? "", /\[redacted-api-key\]/);
  assert.match(lines[0] ?? "", /Keychain account=\[redacted\]/);
  assert.match(lines[0] ?? "", /"authToken":"\[redacted\]"/);
  assert.match(lines[0] ?? "", /"apiKey":"\[redacted\]"/);
  assert.doesNotMatch(lines[0] ?? "", /json-secret|json-api-key/);

  if (process.platform !== "win32") {
    const logDir = path.join(root, "agents", "agent-1", "logs");
    const logFile = path.join(logDir, "agent.log");
    assert.equal((await fs.stat(logDir)).mode & 0o777, 0o700);
    assert.equal((await fs.stat(logFile)).mode & 0o777, 0o600);
  }
});

test("LogStore redacts existing log contents on tail and clamps line count", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-logs-tail-redact-"));
  const logDir = path.join(root, "agents", "agent-1", "logs");
  const logFile = path.join(logDir, "agent.log");
  await fs.mkdir(logDir, { recursive: true, mode: 0o700 });
  await fs.writeFile(
    logFile,
    Array.from({ length: 1105 }, (_, index) => {
      if (index === 1050) return 'legacy authorization: bearer old-secret token=raw-secret {"authToken":"json-secret"}';
      return `line-${index}`;
    }).join("\n"),
    { mode: 0o600 },
  );
  const logs = new LogStore(root);

  const lines = await logs.tail("agent-1", 5000);

  assert.equal(lines.length, 1000);
  assert.equal(lines.at(0), "line-105");
  assert.equal(lines.at(-1), "line-1104");
  assert(lines.some((line) => /authorization: bearer \[redacted\]/i.test(line)));
  assert(!lines.some((line) => line.includes("old-secret") || line.includes("raw-secret") || line.includes("json-secret")));
});

test("LogStore rejects symlinked log directories", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-logs-dir-symlink-"));
  const outside = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-logs-outside-"));
  await fs.mkdir(path.join(root, "agents", "agent-1"), { recursive: true });
  await fs.symlink(outside, path.join(root, "agents", "agent-1", "logs"));
  const logs = new LogStore(root);

  await assert.rejects(logs.append("agent-1", "hello"), /unsafe filesystem path/);
  await assert.rejects(fs.access(path.join(outside, "agent.log")), /ENOENT/);
});

test("LogStore does not append through symlinked log files", { skip: process.platform === "win32" }, async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-logs-file-symlink-"));
  const outside = path.join(root, "outside.log");
  const logDir = path.join(root, "agents", "agent-1", "logs");
  await fs.mkdir(logDir, { recursive: true });
  await fs.writeFile(outside, "do-not-append\n");
  await fs.symlink(outside, path.join(logDir, "agent.log"));
  const logs = new LogStore(root);

  await assert.rejects(logs.append("agent-1", "hello"), /ELOOP|unsafe filesystem path/);
  assert.equal(await fs.readFile(outside, "utf8"), "do-not-append\n");
});

test("LogStore does not read through symlinked log directories", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-logs-read-dir-symlink-"));
  const outside = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-logs-read-outside-"));
  await fs.mkdir(path.join(root, "agents", "agent-1"), { recursive: true });
  await fs.writeFile(path.join(outside, "agent.log"), "external log\n");
  await fs.symlink(outside, path.join(root, "agents", "agent-1", "logs"));
  const logs = new LogStore(root);

  await assert.rejects(logs.tail("agent-1"), /unsafe filesystem path/);
});

test("LogStore does not read through symlinked log files", { skip: process.platform === "win32" }, async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-logs-read-file-symlink-"));
  const outside = path.join(root, "outside.log");
  const logDir = path.join(root, "agents", "agent-1", "logs");
  await fs.mkdir(logDir, { recursive: true });
  await fs.writeFile(outside, "external secret=raw\n");
  await fs.symlink(outside, path.join(logDir, "agent.log"));
  const logs = new LogStore(root);

  await assert.rejects(logs.tail("agent-1"), /ELOOP|unsafe filesystem path/);
});

test("LogStore rejects unsafe agent ids at the log boundary", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-logs-id-boundary-"));
  const logs = new LogStore(root);

  await assert.rejects(logs.append("../outside", "hello"), /invalid agent id/);
  await assert.rejects(logs.tail("../outside"), /invalid agent id/);
  await assert.rejects(logs.append("a".repeat(129), "hello"), /invalid agent id/);
  await assert.rejects(fs.access(path.join(root, "outside", "logs", "agent.log")), /ENOENT/);
});
