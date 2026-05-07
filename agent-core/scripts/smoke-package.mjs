#!/usr/bin/env node
import { spawn } from "node:child_process";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";

const root = process.cwd();
const tmp = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-package-smoke-"));
const prefix = path.join(tmp, "prefix");
const home = path.join(tmp, "home");
const port = String(63587 + Math.floor(Math.random() * 500));
let tarball;
let daemon;

try {
  tarball = (await execFile("npm", ["pack", "--silent"], { cwd: root })).trim().split(/\r?\n/).pop();
  if (!tarball) throw new Error("npm pack did not return a tarball name");

  await execFile("npm", ["install", "-g", "--prefix", prefix, path.join(root, tarball)]);
  const agentBin = process.platform === "win32" ? path.join(prefix, "agent.cmd") : path.join(prefix, "bin", "agent");

  const version = (await execFile(agentBin, ["version"], { env: { ...process.env, AGENT_CORE_HOME: home } })).trim();
  if (!version) throw new Error("packaged agent did not print a version");
  const doctor = JSON.parse(
    await execFile(agentBin, ["doctor"], {
      env: { ...process.env, AGENT_CORE_HOME: home, AGENT_CORE_SERVER_URL: "http://127.0.0.1:9" },
    }),
  );
  assertDoctorReport(doctor);

  daemon = spawn(agentBin, ["start"], {
    env: { ...process.env, AGENT_CORE_HOME: home, AGENT_CORE_PORT: port },
    stdio: ["ignore", "pipe", "pipe"],
  });

  const log = captureOutput(daemon);
  await waitForHealth(port, daemon);
  let token = await waitForControlToken(log, daemon);
  const startJson = JSON.parse(await execFile(agentBin, ["start", "--json"], { env: { ...process.env, AGENT_CORE_HOME: home } }));
  if (startJson.status !== "already_listening" || startJson.url !== `http://127.0.0.1:${port}` || startJson.token !== token) {
    throw new Error(`packaged agent start --json did not report the running daemon: ${stringifySafe(startJson)}`);
  }
  const diagnostics = await fetchJson(`http://127.0.0.1:${port}/diagnostics`, token);
  assertCoreStatus(diagnostics.core);
  const readableRoot = path.join(tmp, "readable-root");
  const writableRoot = path.join(tmp, "writable-root");
  await fs.mkdir(readableRoot);
  await fs.mkdir(writableRoot);
  const canonicalReadableRoot = await fs.realpath(readableRoot);
  const canonicalWritableRoot = await fs.realpath(writableRoot);
  const createPreview = await requestJson(`http://127.0.0.1:${port}/agents/preview`, {
    token,
    method: "POST",
    body: {
      name: "Package Smoke Preview Agent",
      provider: "codex",
      accessMode: "safe",
      deniedTools: ["security"],
      fileAccess: { readablePaths: [readableRoot] },
      providerConfigPaths: [".codex/skills"],
    },
  });
  assertPolicyPreview(createPreview.body, createPreview.body?.agent?.id, {
    deniedTools: ["security"],
    readablePaths: [canonicalReadableRoot],
    providerConfigPaths: [".codex/skills"],
  });
  const emptyAgents = await fetchJson(`http://127.0.0.1:${port}/agents`, token);
  if (emptyAgents.agents?.length !== 0) {
    throw new Error(`agent create preview persisted unexpectedly: ${stringifySafe(emptyAgents)}`);
  }
  const agent = await postJson(`http://127.0.0.1:${port}/agents`, token, {
    name: "Package Smoke Agent",
    provider: "codex",
    accessMode: "safe",
  });
  assertAgent(agent);
  const policyPreview = await fetchJson(`http://127.0.0.1:${port}/agents/${agent.id}/policy`, token);
  assertPolicyPreview(policyPreview, agent.id);
  const proposedPolicyPreview = await requestJson(`http://127.0.0.1:${port}/agents/${agent.id}/policy?preview=true`, {
    token,
    method: "POST",
    body: {
      deniedTools: ["security"],
      fileAccess: { writablePaths: [writableRoot] },
      providerConfigPaths: [".codex/prompts"],
    },
  });
  assertPolicyPreview(proposedPolicyPreview.body, agent.id, {
    deniedTools: ["security"],
    writablePaths: [canonicalWritableRoot],
    providerConfigPaths: [".codex/prompts"],
  });
  const afterPreview = await fetchJson(`http://127.0.0.1:${port}/agents/${agent.id}`, token);
  if (afterPreview.deniedTools?.includes("security")) {
    throw new Error(`policy preview persisted deniedTools unexpectedly: ${stringifySafe(afterPreview)}`);
  }
  if (afterPreview.fileAccess?.writablePaths?.includes(canonicalWritableRoot)) {
    throw new Error(`policy preview persisted fileAccess unexpectedly: ${stringifySafe(afterPreview)}`);
  }
  if (afterPreview.providerConfigPaths?.includes(".codex/prompts")) {
    throw new Error(`policy preview persisted providerConfigPaths unexpectedly: ${stringifySafe(afterPreview)}`);
  }
  await deleteJson(`http://127.0.0.1:${port}/agents/${agent.id}`, token);
  const grant = await postJson(`http://127.0.0.1:${port}/local-grants`, token, { ttlMs: 60_000 });
  assertLocalGrant(grant);
  const grantList = await fetchJson(`http://127.0.0.1:${port}/local-grants`, token);
  assertLocalGrantList(grantList, grant.id, true);
  const grantDiagnostics = await fetchJson(`http://127.0.0.1:${port}/diagnostics`, grant.token);
  assertCoreStatus(grantDiagnostics.core);
  const invalidPairingStart = await requestJson(`http://127.0.0.1:${port}/pairing/start`, {
    token,
    method: "POST",
    body: [],
    expectedStatus: 400,
  });
  if (invalidPairingStart.body?.error?.code !== "bad_request") {
    throw new Error(`invalid pairing start did not fail as bad_request: ${stringifySafe(invalidPairingStart.body)}`);
  }
  const invalidPairing = await requestJson(`http://127.0.0.1:${port}/pairing/confirm`, {
    token,
    method: "POST",
    body: { pairingId: "package-smoke" },
    expectedStatus: 400,
  });
  if (invalidPairing.body?.error?.code !== "bad_request") {
    throw new Error(`invalid pairing confirm did not fail as bad_request: ${stringifySafe(invalidPairing.body)}`);
  }
  await deleteJson(`http://127.0.0.1:${port}/local-grants/${grant.id}`, token);
  const revokedGrantList = await fetchJson(`http://127.0.0.1:${port}/local-grants`, token);
  assertLocalGrantList(revokedGrantList, grant.id, false);
  const revokedGrantResponse = await requestJson(`http://127.0.0.1:${port}/diagnostics`, {
    token: grant.token,
    expectedStatus: 401,
  });
  if (revokedGrantResponse.body?.error?.code !== "unauthorized") {
    throw new Error(`revoked local grant did not fail as unauthorized: ${stringifySafe(revokedGrantResponse.body)}`);
  }

  const rotateOutput = await execFile(agentBin, ["rotate-token"], { env: { ...process.env, AGENT_CORE_HOME: home } });
  const rotatedToken = parseControlToken(rotateOutput);
  if (!rotatedToken || rotatedToken === token) {
    throw new Error(`packaged agent rotate-token did not print a new token: ${rotateOutput}`);
  }
  const oldTokenResponse = await requestJson(`http://127.0.0.1:${port}/diagnostics`, {
    token,
    expectedStatus: 401,
  });
  if (oldTokenResponse.body?.error?.code !== "unauthorized") {
    throw new Error(`old local control token did not fail after rotation: ${stringifySafe(oldTokenResponse.body)}`);
  }
  token = rotatedToken;
  const rotatedDiagnostics = await fetchJson(`http://127.0.0.1:${port}/diagnostics`, token);
  assertCoreStatus(rotatedDiagnostics.core);

  await execFile(agentBin, ["stop"], { env: { ...process.env, AGENT_CORE_HOME: home } });
  await waitForExit(daemon);
  daemon = undefined;

  console.log(`package smoke passed: agent ${version}, health http://127.0.0.1:${port}/health`);
  if (log.stderr.trim()) console.error(log.stderr.trim());
} finally {
  if (daemon) {
    daemon.kill("SIGTERM");
    await waitForExit(daemon).catch(() => {});
  }
  if (tarball) await fs.rm(path.join(root, tarball), { force: true });
  await fs.rm(tmp, { recursive: true, force: true });
}

function captureOutput(child) {
  const log = { stdout: "", stderr: "" };
  child.stdout?.on("data", (chunk) => {
    log.stdout = `${log.stdout}${String(chunk)}`.slice(-8192);
  });
  child.stderr?.on("data", (chunk) => {
    log.stderr = `${log.stderr}${String(chunk)}`.slice(-8192);
  });
  return log;
}

async function waitForControlToken(log, child) {
  const deadline = Date.now() + 10_000;
  while (Date.now() < deadline) {
    if (child.exitCode !== null || child.signalCode !== null) {
      throw new Error("agent daemon exited before local control token was printed");
    }
    const token = parseControlToken(log.stdout);
    if (token) return token;
    await delay(50);
  }
  throw new Error("agent daemon did not print a local control token");
}

function parseControlToken(stdout) {
  return stdout.match(/^local control token:\s*(\S+)/m)?.[1];
}

async function waitForHealth(port, child) {
  const deadline = Date.now() + 10_000;
  let lastError;
  while (Date.now() < deadline) {
    if (child.exitCode !== null || child.signalCode !== null) {
      throw new Error(`agent daemon exited before health was ready`);
    }
    try {
      const response = await fetch(`http://127.0.0.1:${port}/health`);
      const body = await response.json();
      if (response.ok && body.ok === true && body.authRequired === true) return body;
    } catch (error) {
      lastError = error;
    }
    await delay(250);
  }
  throw new Error(`agent health did not become ready: ${lastError instanceof Error ? lastError.message : String(lastError)}`);
}

async function fetchJson(url, token) {
  const { body } = await requestJson(url, { token });
  return body;
}

async function postJson(url, token, body) {
  const response = await requestJson(url, {
    token,
    method: "POST",
    expectedStatus: 201,
    body,
  });
  return response.body;
}

async function deleteJson(url, token) {
  const response = await requestJson(url, {
    token,
    method: "DELETE",
    expectedStatus: 200,
  });
  return response.body;
}

async function requestJson(url, { token, method = "GET", body, expectedStatus = 200 }) {
  const headers = { Authorization: `Bearer ${token}` };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(url, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const responseBody = await response.json();
  if (response.status !== expectedStatus) {
    throw new Error(`${method} ${url} failed with ${response.status}: ${stringifySafe(responseBody)}`);
  }
  return { status: response.status, body: responseBody };
}

function assertLocalGrant(grant) {
  if (!grant || typeof grant !== "object") throw new Error(`invalid local grant response: ${stringifySafe(grant)}`);
  if (!/^[0-9a-f-]+$/i.test(grant.id ?? "")) throw new Error(`local grant id is invalid: ${stringifySafe(grant)}`);
  if (!/^[A-Za-z0-9_-]+$/.test(grant.token ?? "")) throw new Error(`local grant token is invalid: ${stringifySafe(grant)}`);
  if (!Array.isArray(grant.scopes) || grant.scopes.length !== 1 || grant.scopes[0] !== "management") {
    throw new Error(`local grant scopes are invalid: ${stringifySafe(grant)}`);
  }
  if (Number.isNaN(Date.parse(grant.expiresAt))) throw new Error(`local grant expiresAt is invalid: ${stringifySafe(grant)}`);
}

function assertLocalGrantList(body, id, active) {
  if (!Array.isArray(body?.grants)) throw new Error(`local grant list is invalid: ${stringifySafe(body)}`);
  const grant = body.grants.find((candidate) => candidate?.id === id);
  if (!grant) throw new Error(`local grant list did not include ${id}: ${stringifySafe(body)}`);
  if (grant.active !== active) throw new Error(`local grant active state was invalid: ${stringifySafe(grant)}`);
  if ("token" in grant || "tokenHash" in grant) throw new Error(`local grant list exposed token material: ${stringifySafe(grant)}`);
}

function assertAgent(agent) {
  if (!agent || typeof agent !== "object") throw new Error(`invalid agent response: ${stringifySafe(agent)}`);
  if (!/^[A-Za-z0-9_-]{1,128}$/.test(agent.id ?? "")) throw new Error(`agent id is invalid: ${stringifySafe(agent)}`);
  if (agent.provider !== "codex") throw new Error(`agent provider is invalid: ${stringifySafe(agent)}`);
}

function assertPolicyPreview(body, agentId, expected = {}) {
  if (body?.agent?.id !== agentId) throw new Error(`policy preview did not include the agent: ${stringifySafe(body)}`);
  if (body?.policy?.accessMode !== "safe") throw new Error(`policy preview access mode is invalid: ${stringifySafe(body)}`);
  if (typeof body?.policy?.cwd !== "string") throw new Error(`policy preview cwd is invalid: ${stringifySafe(body)}`);
  if (expected.deniedTools && JSON.stringify(body?.policy?.deniedTools) !== JSON.stringify(expected.deniedTools)) {
    throw new Error(`policy preview deniedTools are invalid: ${stringifySafe(body)}`);
  }
  if (expected.readablePaths && JSON.stringify(body?.policy?.fileAccess?.readablePaths) !== JSON.stringify(expected.readablePaths)) {
    throw new Error(`policy preview readablePaths are invalid: ${stringifySafe(body)}`);
  }
  if (expected.writablePaths && JSON.stringify(body?.policy?.fileAccess?.writablePaths) !== JSON.stringify(expected.writablePaths)) {
    throw new Error(`policy preview writablePaths are invalid: ${stringifySafe(body)}`);
  }
  if (
    expected.providerConfigPaths &&
    JSON.stringify(body?.policy?.providerConfigPaths) !== JSON.stringify(expected.providerConfigPaths)
  ) {
    throw new Error(`policy preview providerConfigPaths are invalid: ${stringifySafe(body)}`);
  }
  if ("env" in (body?.policy ?? {})) throw new Error(`policy preview exposed provider env: ${stringifySafe(body)}`);
}

function assertCoreStatus(core) {
  if (process.env.AGENT_CORE_NATIVE_BUILD_PROFILE === "prod") {
    if (core?.deviceId !== "native-sidecar-prod") {
      throw new Error(`prod package did not start the production native sidecar: ${stringifySafe(core)}`);
    }
    if (core.status === "pairing_required" && core.authTokenSource === "keychain") {
      if (
        !Array.isArray(core.blockedBy) ||
        !core.blockedBy.includes("backend_pairing_contract") ||
        !core.blockedBy.includes("space_invite_sync_contract")
      ) {
        throw new Error(`prod package core status did not expose backend blockers: ${stringifySafe(core)}`);
      }
      if (
        !String(core.reason ?? "").includes("backend PR #25") ||
        !String(core.reason ?? "").includes("from dev")
      ) {
        throw new Error(`prod package core status did not expose PR #25 blocker reason: ${stringifySafe(core)}`);
      }
      if (
        !String(core.nextAction ?? "").includes("backend PR #25") ||
        !String(core.nextAction ?? "").includes("from dev") ||
        !String(core.nextAction ?? "").includes("production agent identity publication")
      ) {
        throw new Error(`prod package core status did not expose nextAction: ${stringifySafe(core)}`);
      }
      return;
    }
    if (
      core.status !== "unavailable" ||
      !String(core.reason ?? "").includes("production native profile missing required configuration") ||
      !Array.isArray(core.missingConfig) ||
      core.missingConfig.length === 0 ||
      !Array.isArray(core.blockedBy) ||
      core.blockedBy.length === 0 ||
      typeof core.nextAction !== "string"
    ) {
      throw new Error(`prod package core status did not report missing production config: ${stringifySafe(core)}`);
    }
    return;
  }
  if (core?.status !== "ready" || core.deviceId !== "native-sidecar-dev_mock") {
    throw new Error(`dev package did not start the dev native sidecar: ${stringifySafe(core)}`);
  }
}

function assertDoctorReport(report) {
  if (!report || typeof report !== "object") throw new Error(`invalid doctor report: ${stringifySafe(report)}`);
  assertCoreStatus(report.core);
  if (!report.environment || typeof report.environment !== "object") {
    throw new Error(`doctor report did not include environment: ${stringifySafe(report)}`);
  }
  if (report.environment.server?.url !== "http://127.0.0.1:9") {
    throw new Error(`doctor report did not use the configured server URL: ${stringifySafe(report.environment.server)}`);
  }
  if (!report.environment.providers?.claude || !report.environment.providers?.codex) {
    throw new Error(`doctor report did not include provider checks: ${stringifySafe(report.environment.providers)}`);
  }
}

function stringifySafe(value) {
  return JSON.stringify(value, (_key, item) => {
    if (typeof item === "string" && /^(?:[A-Za-z0-9_-]{24,}|sk-[A-Za-z0-9_-]+)$/.test(item)) {
      return "[redacted]";
    }
    return item;
  });
}

function waitForExit(child) {
  if (child.exitCode !== null || child.signalCode !== null) return Promise.resolve();
  return new Promise((resolve) => {
    child.once("exit", resolve);
  });
}

function execFile(command, args, options = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, {
      cwd: options.cwd,
      env: options.env ?? process.env,
      stdio: ["ignore", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => {
      stdout += String(chunk);
    });
    child.stderr.on("data", (chunk) => {
      stderr += String(chunk);
    });
    child.on("error", reject);
    child.on("exit", (code, signal) => {
      if (code === 0) return resolve(stdout);
      reject(new Error(`${command} ${args.join(" ")} failed code=${code ?? "null"} signal=${signal ?? "null"}\n${stderr}`));
    });
  });
}

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
