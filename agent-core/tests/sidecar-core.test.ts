import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import type { DeviceStatus } from "../src/native/core.js";
import { SidecarCoreNative, sidecarLaunchCommand } from "../src/native/sidecar-core.js";

test("SidecarCoreNative keeps core state across native requests", async () => {
  const core = new SidecarCoreNative();
  try {
    const device = await core.openOrCreateDevice();
    assert.equal(device.connected, true);
    assert.equal(device.status, "ready");
    const pairing = await core.startPairing({});
    assert.equal(pairing.status, "unavailable");
    assert.deepEqual(pairing.blockedBy, ["daemon_pairing_gateway"]);

    const identity = await core.createAgentIdentity({
      operatorSlug: "alice",
      agentSlug: "alice-agent",
    });
    assert.equal(identity.identityType, "agent");
    assert.match(identity.declaredOperatorPublicKey ?? "", /.+/);

    const handle = await core.openAgentSession({ agentSlug: "alice-agent" });
    assert.equal(handle, "session:alice-agent");

    const injected = await core.devInjectChannelMessage({
      senderSlug: "alice",
      agentSlug: "alice-agent",
      body: "@alice-agent status?",
    });
    assert.match(injected.messageId, /^msg_/);

    const messages = await core.processPendingMessages(handle);
    assert.equal(messages.length, 1);
    assert.equal(messages[0]?.body, "@alice-agent status?");
    assert.equal(messages[0]?.mustRespond, true);
    assert.equal(messages[0]?.senderSlug, "alice");

    const reply = await core.sendChannelReply(handle, {
      spaceId: injected.spaceId,
      channelId: injected.channelId,
      body: "ack",
      replyToId: messages[0]?.id,
    });
    assert.match(reply.messageId, /^msg_/);

    const repeated = await core.processPendingMessages(handle);
    assert.equal(repeated.length, 0);
  } finally {
    await core.shutdown();
  }
});

test("SidecarCoreNative times out a hung native request", { skip: process.platform === "win32" }, async () => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-sidecar-timeout-"));
  const script = path.join(dir, "silent-sidecar.sh");
  await fs.writeFile(script, "#!/bin/sh\nsleep 5\n", { mode: 0o700 });
  const core = new SidecarCoreNative({ binaryPath: script, requestTimeoutMs: 25 });
  try {
    await assert.rejects(core.openOrCreateDevice(), /native sidecar request timed out: health/);
  } finally {
    await core.shutdown();
  }
});

test("SidecarCoreNative rejects malformed native RPC envelopes", { skip: process.platform === "win32" }, async () => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-sidecar-envelope-"));
  const script = path.join(dir, "envelope-sidecar.js");
  await fs.writeFile(
    script,
    [
      "#!/usr/bin/env node",
      "process.stdin.setEncoding('utf8');",
      "process.stdin.on('data', (chunk) => {",
      "  for (const line of String(chunk).trim().split(/\\n+/)) {",
      "    if (!line) continue;",
      "    const request = JSON.parse(line);",
      "    process.stdout.write(JSON.stringify({",
      "      id: request.id,",
      "      ok: 'yes',",
      "      result: { connected: true, status: 'ready' }",
      "    }) + '\\n');",
      "  }",
      "});",
    ].join("\n"),
    { mode: 0o700 },
  );
  const core = new SidecarCoreNative({ binaryPath: script, requestTimeoutMs: 500 });
  try {
    await assert.rejects(
      core.openOrCreateDevice(),
      /native sidecar response ok must be boolean/,
    );
  } finally {
    await core.shutdown();
    await fs.rm(dir, { recursive: true, force: true });
  }
});

test("SidecarCoreNative rejects malformed native response shapes", { skip: process.platform === "win32" }, async () => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-sidecar-malformed-"));
  const script = path.join(dir, "malformed-sidecar.js");
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
      "    const result = request.method === 'createAgentIdentity'",
      "      ? request.params.agentSlug === 'oversized-agent'",
      "        ? { ok: true, operatorSlug: 'alice', agentSlug: 'oversized-agent', identityType: 'agent', declaredOperatorPublicKey: 'k'.repeat(4097) }",
      "        : { ok: true, operatorSlug: 'alice', agentSlug: 'alice-agent', identityType: 'agent' }",
      "      : {};",
      "    process.stdout.write(JSON.stringify({ id: request.id, ok: true, result }) + '\\n');",
      "  }",
      "});",
    ].join("\n"),
    { mode: 0o700 },
  );
  const core = new SidecarCoreNative({ binaryPath: script, requestTimeoutMs: 500 });
  try {
    await assert.rejects(
      core.openOrCreateDevice(),
      /health response\.connected must be boolean/,
    );
    await assert.rejects(
      core.startPairing({}),
      /startPairing response\.status/,
    );
    await assert.rejects(
      core.createAgentIdentity({ operatorSlug: "alice", agentSlug: "alice-agent" }),
      /declaredOperatorPublicKey is required/,
    );
    await assert.rejects(
      core.createAgentIdentity({ operatorSlug: "alice", agentSlug: "oversized-agent" }),
      /declaredOperatorPublicKey.*4096/,
    );
    await assert.rejects(
      core.openAgentSession({ agentSlug: "alice-agent" }),
      /handle is required/,
    );
    await assert.rejects(
      core.sendChannelReply("handle" as any, { spaceId: "space", channelId: "channel", body: "hello" }),
      /messageId is required/,
    );
  } finally {
    await core.shutdown();
    await fs.rm(dir, { recursive: true, force: true });
  }
});

test("SidecarCoreNative starts native sidecars with a minimal env", { skip: process.platform === "win32" }, async () => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-sidecar-env-"));
  const script = path.join(dir, "env-sidecar.js");
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
      "          openai: process.env.OPENAI_API_KEY ?? null,",
      "          anthropic: process.env.ANTHROPIC_API_KEY ?? null,",
      "          agentCoreAuth: process.env.AGENT_CORE_AUTH_TOKEN ?? null,",
      "          agentCoreServer: process.env.AGENT_CORE_SERVER_URL ?? null,",
      "          agentCoreDb: process.env.AGENT_CORE_DATABASE_PATH ?? null,",
      "          agentCoreSidecarBin: process.env.AGENT_CORE_SIDECAR_BIN ?? null,",
      "          path: process.env.PATH ?? null",
      "        }",
      "      }",
      "    }) + '\\n');",
      "  }",
      "});",
    ].join("\n"),
    { mode: 0o700 },
  );
  const previousOpenAi = process.env.OPENAI_API_KEY;
  const previousAnthropic = process.env.ANTHROPIC_API_KEY;
  const previousAuth = process.env.AGENT_CORE_AUTH_TOKEN;
  const previousServer = process.env.AGENT_CORE_SERVER_URL;
  const previousDbPath = process.env.AGENT_CORE_DATABASE_PATH;
  const previousSidecarBin = process.env.AGENT_CORE_SIDECAR_BIN;
  const stateHome = path.join(dir, "state-home");
  process.env.OPENAI_API_KEY = "openai-token";
  process.env.ANTHROPIC_API_KEY = "anthropic-token";
  process.env.AGENT_CORE_AUTH_TOKEN = "sidecar-token";
  process.env.AGENT_CORE_SERVER_URL = "https://api.example.test";
  delete process.env.AGENT_CORE_DATABASE_PATH;
  process.env.AGENT_CORE_SIDECAR_BIN = "/tmp/sidecar";
  const core = new SidecarCoreNative({ binaryPath: script, stateHome });

  try {
    const device = await core.openOrCreateDevice() as DeviceStatus & {
      env: Record<string, string | null>;
    };
    assert.equal(device.env.openai, null);
    assert.equal(device.env.anthropic, null);
    assert.equal(device.env.agentCoreSidecarBin, null);
    assert.equal(device.env.agentCoreAuth, "sidecar-token");
    assert.equal(device.env.agentCoreServer, "https://api.example.test");
    assert.equal(device.env.agentCoreDb, path.join(stateHome, "core.sqlite"));
    assert.equal(typeof device.env.path, "string");
  } finally {
    await core.shutdown();
    await fs.rm(dir, { recursive: true, force: true });
    restoreEnv("OPENAI_API_KEY", previousOpenAi);
    restoreEnv("ANTHROPIC_API_KEY", previousAnthropic);
    restoreEnv("AGENT_CORE_AUTH_TOKEN", previousAuth);
    restoreEnv("AGENT_CORE_SERVER_URL", previousServer);
    restoreEnv("AGENT_CORE_DATABASE_PATH", previousDbPath);
    restoreEnv("AGENT_CORE_SIDECAR_BIN", previousSidecarBin);
  }
});

test("SidecarCoreNative reports explicit production profile configuration gaps", async () => {
  const previousProfile = process.env.AGENT_CORE_NATIVE_PROFILE;
  const previousServer = process.env.AGENT_CORE_SERVER_URL;
  const previousDbPath = process.env.AGENT_CORE_DATABASE_PATH;
  const previousAuth = process.env.AGENT_CORE_AUTH_TOKEN;
  process.env.AGENT_CORE_NATIVE_PROFILE = "prod";
  delete process.env.AGENT_CORE_SERVER_URL;
  delete process.env.AGENT_CORE_DATABASE_PATH;
  delete process.env.AGENT_CORE_AUTH_TOKEN;
  const core = new SidecarCoreNative();
  try {
    const device = await core.openOrCreateDevice();
    assert.equal(device.connected, false);
    assert.equal(device.status, "unavailable");
    assert.match(device.reason ?? "", /AGENT_CORE_AUTH_TOKEN/);
    assert.deepEqual(device.missingConfig, ["AGENT_CORE_AUTH_TOKEN"]);
    assert.deepEqual(device.blockedBy, ["AGENT_CORE_AUTH_TOKEN"]);
    assert.match(device.nextAction ?? "", /server-issued authToken/);
    const pairing = await core.startPairing({});
    assert.equal(pairing.status, "unavailable");
    assert.deepEqual(pairing.blockedBy, ["daemon_pairing_gateway"]);
  } finally {
    await core.shutdown();
    restoreEnv("AGENT_CORE_NATIVE_PROFILE", previousProfile);
    restoreEnv("AGENT_CORE_SERVER_URL", previousServer);
    restoreEnv("AGENT_CORE_DATABASE_PATH", previousDbPath);
    restoreEnv("AGENT_CORE_AUTH_TOKEN", previousAuth);
  }
});

test("SidecarCoreNative can activate a production auth token through confirmPairing", async () => {
  const previousProfile = process.env.AGENT_CORE_NATIVE_PROFILE;
  const previousServer = process.env.AGENT_CORE_SERVER_URL;
  const previousDbPath = process.env.AGENT_CORE_DATABASE_PATH;
  const previousAuth = process.env.AGENT_CORE_AUTH_TOKEN;
  process.env.AGENT_CORE_NATIVE_PROFILE = "prod";
  delete process.env.AGENT_CORE_SERVER_URL;
  delete process.env.AGENT_CORE_DATABASE_PATH;
  delete process.env.AGENT_CORE_AUTH_TOKEN;
  const core = new SidecarCoreNative();
  try {
    const before = await core.openOrCreateDevice();
    assert.equal(before.status, "unavailable");
    assert.deepEqual(before.missingConfig, ["AGENT_CORE_AUTH_TOKEN"]);

    const confirmed = await core.confirmPairing({ authToken: "stored-token" }) as DeviceStatus & {
      authTokenSource?: string;
    };
    assert.equal(confirmed.status, "pairing_required");
    assert.equal(confirmed.deviceId, "native-sidecar-prod");
    assert.match(confirmed.authTokenSource ?? "", /^(keychain|memory)$/);
    assert.ok(confirmed.blockedBy?.includes("backend_pairing_contract"));
    assert.match(confirmed.nextAction ?? "", /backend PR #26/);
    assert.match(confirmed.nextAction ?? "", /backend PR #25/);
    assert.match(confirmed.nextAction ?? "", /from dev/);
    assert.match(confirmed.nextAction ?? "", /production agent identity publication/);

    const after = await core.openOrCreateDevice();
    assert.equal(after.status, "pairing_required");
    assert.ok(after.blockedBy?.includes("space_invite_sync_contract"));
    assert.match(after.reason ?? "", /backend PR #26/);
    assert.match(after.reason ?? "", /backend PR #25/);
    assert.match(after.reason ?? "", /from dev/);
  } finally {
    await core.shutdown();
    restoreEnv("AGENT_CORE_NATIVE_PROFILE", previousProfile);
    restoreEnv("AGENT_CORE_SERVER_URL", previousServer);
    restoreEnv("AGENT_CORE_DATABASE_PATH", previousDbPath);
    restoreEnv("AGENT_CORE_AUTH_TOKEN", previousAuth);
  }
});

test("SidecarCoreNative rejects malformed pairing confirmation input before native spawn", async () => {
  const core = new SidecarCoreNative();
  await assert.rejects(
    core.confirmPairing(null as unknown as Record<string, unknown>),
    /pairing confirmation body must be an object/,
  );
  await assert.rejects(
    core.confirmPairing({ authToken: "stored-token", pairingId: "../bad" }),
    /pairingId must be a safe identifier/,
  );
  await assert.rejects(
    core.confirmPairing({ authToken: "a".repeat(16 * 1024 + 1) }),
    /authToken must be a non-empty string/,
  );
  await assert.rejects(
    core.confirmPairing({ authToken: "stored-token", operatorBootstrap: { kind: "restore_or_enroll" } }),
    /operatorBootstrap is not supported/,
  );
});

test("SidecarCoreNative rejects malformed pairing start input before native spawn", async () => {
  const core = new SidecarCoreNative();
  await assert.rejects(
    core.startPairing([] as unknown as Record<string, unknown>),
    /native startPairing body must be an object/,
  );
  await assert.rejects(
    core.startPairing({ pairingPublicNonce: "short nonce" }),
    /pairingPublicNonce must be an unpadded base64url/,
  );
  await assert.rejects(
    core.startPairing({ localApiOrigin: "https://evil.example.test" }),
    /localApiOrigin is not supported/,
  );
});

test("SidecarCoreNative accepts complete production profile configuration without using dev mock", async () => {
  const previousProfile = process.env.AGENT_CORE_NATIVE_PROFILE;
  const previousServer = process.env.AGENT_CORE_SERVER_URL;
  const previousDbPath = process.env.AGENT_CORE_DATABASE_PATH;
  const previousAuth = process.env.AGENT_CORE_AUTH_TOKEN;
  process.env.AGENT_CORE_NATIVE_PROFILE = "prod";
  process.env.AGENT_CORE_SERVER_URL = "https://api.example.test";
  process.env.AGENT_CORE_DATABASE_PATH = path.join(os.tmpdir(), "agent-core-prod-profile.sqlite");
  process.env.AGENT_CORE_AUTH_TOKEN = "test-token";
  const core = new SidecarCoreNative();
  try {
    const device = await core.openOrCreateDevice();
    assert.equal(device.connected, false);
    assert.equal(device.status, "pairing_required");
    assert.match(device.reason ?? "", /production native profile parsed/);
    assert.match(device.reason ?? "", /backend PR #26/);
    assert.match(device.reason ?? "", /backend PR #25/);
    assert.doesNotMatch(device.reason ?? "", /session wiring are not implemented/);
    assert.ok(device.blockedBy?.includes("backend_pairing_contract"));
    assert.ok(!device.blockedBy?.includes("agent_identity_publication_contract"));
    assert.match(device.nextAction ?? "", /backend PR #26/);
    assert.match(device.nextAction ?? "", /backend PR #25/);
    assert.match(device.nextAction ?? "", /from dev/);
    assert.match(device.nextAction ?? "", /account-bound sync/);
    assert.notEqual(device.deviceId, "native-sidecar-dev_mock");
  } finally {
    await core.shutdown();
    restoreEnv("AGENT_CORE_NATIVE_PROFILE", previousProfile);
    restoreEnv("AGENT_CORE_SERVER_URL", previousServer);
    restoreEnv("AGENT_CORE_DATABASE_PATH", previousDbPath);
    restoreEnv("AGENT_CORE_AUTH_TOKEN", previousAuth);
  }
});

test("SidecarCoreNative routes production agent identity publication through native provider construction", async () => {
  const previousProfile = process.env.AGENT_CORE_NATIVE_PROFILE;
  const previousServer = process.env.AGENT_CORE_SERVER_URL;
  const previousDbPath = process.env.AGENT_CORE_DATABASE_PATH;
  const previousAuth = process.env.AGENT_CORE_AUTH_TOKEN;
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-prod-agent-"));
  process.env.AGENT_CORE_NATIVE_PROFILE = "prod";
  process.env.AGENT_CORE_SERVER_URL = "https://api.example.test";
  process.env.AGENT_CORE_DATABASE_PATH = path.join(dir, "agent-core.sqlite");
  process.env.AGENT_CORE_AUTH_TOKEN = "test-token";
  const core = new SidecarCoreNative();
  try {
    await assert.rejects(
      core.createAgentIdentity({ operatorSlug: "alice", agentSlug: "alice-agent" }),
      /production native profile requires agent-native built with apple-keychain|local identity missing/,
    );
  } finally {
    await core.shutdown();
    await fs.rm(dir, { recursive: true, force: true });
    restoreEnv("AGENT_CORE_NATIVE_PROFILE", previousProfile);
    restoreEnv("AGENT_CORE_SERVER_URL", previousServer);
    restoreEnv("AGENT_CORE_DATABASE_PATH", previousDbPath);
    restoreEnv("AGENT_CORE_AUTH_TOKEN", previousAuth);
  }
});

test("SidecarCoreNative validates agent identity and session slugs before native spawn", async () => {
  const core = new SidecarCoreNative();
  await assert.rejects(
    core.createAgentIdentity({ operatorSlug: "Alice", agentSlug: "alice-agent" }),
    /operatorSlug must be a lowercase slug/,
  );
  await assert.rejects(
    core.createAgentIdentity({ operatorSlug: "alice", agentSlug: "bad slug" }),
    /agentSlug must be a lowercase slug/,
  );
  await assert.rejects(
    core.openAgentSession(null as unknown as Record<string, unknown>),
    /openAgentSession input must be an object/,
  );
  await assert.rejects(
    core.openAgentSession({ agentSlug: "bad slug" }),
    /agentSlug must be a lowercase slug/,
  );
});

test("SidecarCoreNative validates message RPC inputs before native spawn", async () => {
  const core = new SidecarCoreNative();
  await assert.rejects(
    core.syncOnce("" as any),
    /native session handle is required/,
  );
  await assert.rejects(
    core.sendChannelReply("handle" as any, null as unknown as Record<string, unknown>),
    /sendChannelReply input must be an object/,
  );
  await assert.rejects(
    core.sendDirectReply("handle" as any, { recipientSlug: "Alice", body: "hello" }),
    /recipientSlug must be a lowercase slug/,
  );
  await assert.rejects(
    core.devInjectChannelMessage({ senderSlug: "Alice", agentSlug: "alice-agent", body: "hello" }),
    /senderSlug must be a lowercase slug/,
  );
});

test("SidecarCoreNative routes production session requests through native provider construction", async () => {
  const previousProfile = process.env.AGENT_CORE_NATIVE_PROFILE;
  const previousServer = process.env.AGENT_CORE_SERVER_URL;
  const previousDbPath = process.env.AGENT_CORE_DATABASE_PATH;
  const previousAuth = process.env.AGENT_CORE_AUTH_TOKEN;
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-prod-session-"));
  process.env.AGENT_CORE_NATIVE_PROFILE = "prod";
  process.env.AGENT_CORE_SERVER_URL = "https://api.example.test";
  process.env.AGENT_CORE_DATABASE_PATH = path.join(dir, "agent-core.sqlite");
  process.env.AGENT_CORE_AUTH_TOKEN = "test-token";
  const core = new SidecarCoreNative();
  try {
    await assert.rejects(
      core.openAgentSession({ agentSlug: "alice-agent" }),
      /production native profile requires agent-native built with apple-keychain|local identity missing/,
    );
  } finally {
    await core.shutdown();
    await fs.rm(dir, { recursive: true, force: true });
    restoreEnv("AGENT_CORE_NATIVE_PROFILE", previousProfile);
    restoreEnv("AGENT_CORE_SERVER_URL", previousServer);
    restoreEnv("AGENT_CORE_DATABASE_PATH", previousDbPath);
    restoreEnv("AGENT_CORE_AUTH_TOKEN", previousAuth);
  }
});

test("SidecarCoreNative can serve the production profile without dev-tools", async () => {
  const previousProfile = process.env.AGENT_CORE_NATIVE_PROFILE;
  const previousServer = process.env.AGENT_CORE_SERVER_URL;
  const previousDbPath = process.env.AGENT_CORE_DATABASE_PATH;
  const previousAuth = process.env.AGENT_CORE_AUTH_TOKEN;
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-prod-no-dev-"));
  delete process.env.AGENT_CORE_NATIVE_PROFILE;
  process.env.AGENT_CORE_SERVER_URL = "https://api.example.test";
  process.env.AGENT_CORE_DATABASE_PATH = path.join(dir, "agent-core.sqlite");
  process.env.AGENT_CORE_AUTH_TOKEN = "test-token";
  const core = new SidecarCoreNative({ features: [], preferPackagedBinary: false });
  try {
    const device = await core.openOrCreateDevice();
    assert.equal(device.status, "pairing_required");
    assert.equal(device.deviceId, "native-sidecar-prod");
    await assert.rejects(
      core.openAgentSession({ agentSlug: "alice-agent" }),
      /production native profile requires agent-native built with apple-keychain/,
    );
  } finally {
    await core.shutdown();
    await fs.rm(dir, { recursive: true, force: true });
    restoreEnv("AGENT_CORE_NATIVE_PROFILE", previousProfile);
    restoreEnv("AGENT_CORE_SERVER_URL", previousServer);
    restoreEnv("AGENT_CORE_DATABASE_PATH", previousDbPath);
    restoreEnv("AGENT_CORE_AUTH_TOKEN", previousAuth);
  }
});

test("sidecar launcher reports a missing packaged native binary clearly", async () => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-sidecar-missing-"));
  const missingManifest = path.join(dir, "missing", "Cargo.toml");
  const launch = sidecarLaunchCommand({
    manifestPath: missingManifest,
    features: ["dev-tools"],
  });
  assert.match(launch.diagnostic ?? "", /native sidecar binary is unavailable/);

  const core = new SidecarCoreNative({
    manifestPath: missingManifest,
    requestTimeoutMs: 25,
    preferPackagedBinary: false,
  });
  await assert.rejects(core.openOrCreateDevice(), /native sidecar binary is unavailable/);
});

test("sidecar launcher rejects relative native binary overrides", async () => {
  const launch = sidecarLaunchCommand({
    manifestPath: path.join(os.tmpdir(), "Cargo.toml"),
    features: ["dev-tools"],
    binaryPath: "relative-sidecar",
  });

  assert.match(launch.diagnostic ?? "", /must be absolute/);

  const core = new SidecarCoreNative({ binaryPath: "relative-sidecar", requestTimeoutMs: 25 });
  await assert.rejects(core.openOrCreateDevice(), /must be absolute/);
});

test("sidecar launcher reports missing native binary overrides before spawn", async () => {
  const missing = path.join(os.tmpdir(), "agent-core-missing-sidecar-bin");
  const launch = sidecarLaunchCommand({
    manifestPath: path.join(os.tmpdir(), "Cargo.toml"),
    features: ["dev-tools"],
    binaryPath: missing,
  });

  assert.match(launch.diagnostic ?? "", /does not exist/);

  const core = new SidecarCoreNative({ binaryPath: missing, requestTimeoutMs: 25 });
  await assert.rejects(core.openOrCreateDevice(), /does not exist/);
});

function restoreEnv(name: string, value: string | undefined): void {
  if (value === undefined) delete process.env[name];
  else process.env[name] = value;
}
