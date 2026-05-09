import assert from "node:assert/strict";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import fs from "node:fs/promises";
import test from "node:test";
import { isLoopbackHost } from "../src/api/auth.js";
import { ApiServer } from "../src/api/server.js";
import { ProviderDetector } from "../src/doctor/provider-detector.js";
import { LogStore } from "../src/logs/log-store.js";
import { CoreNative, DeviceStatus, UnavailableCoreNative } from "../src/native/core.js";
import { SidecarCoreNative } from "../src/native/sidecar-core.js";
import { ServerPairingGateway } from "../src/pairing/server-pairing.js";
import { ProviderSession } from "../src/providers/provider-types.js";
import { RuntimeManager } from "../src/runtime/runtime-manager.js";
import { StateStore } from "../src/state/store.js";
import { CommandResult, CommandRunner } from "../src/platform/shell.js";
import { AgentInput, AgentOutput, ProviderStatus } from "../src/types.js";

class EmptyRunner implements CommandRunner {
  async run(): Promise<CommandResult> {
    return { code: 1, stdout: "", stderr: "" };
  }
}

class PathRunner implements CommandRunner {
  async run(command: string, args: string[] = []): Promise<CommandResult> {
    if (command === "which" && args[0] === "claude") {
      return { code: 0, stdout: "/Users/alice/.local/bin/claude\n", stderr: "" };
    }
    if (command === "which" && args[0] === "codex") {
      return { code: 0, stdout: "/Users/alice/.local/bin/codex\n", stderr: "" };
    }
    if (command === "which" && args[0] === "sandbox-exec") {
      return { code: 0, stdout: "/usr/bin/sandbox-exec\n", stderr: "" };
    }
    if (command.endsWith("/claude")) return { code: 0, stdout: "2.1.121\n", stderr: "" };
    if (command.endsWith("/codex")) return { code: 0, stdout: "codex-cli 0.128.0\n", stderr: "" };
    return { code: 1, stdout: "", stderr: "" };
  }
}

function testDetector(): ProviderDetector {
  return new ProviderDetector(new EmptyRunner(), { checkServer: false });
}

function pathDetector(): ProviderDetector {
  return new ProviderDetector(new PathRunner(), { checkServer: false });
}

test("isLoopbackHost only accepts exact loopback hosts", () => {
  assert.equal(isLoopbackHost(undefined), true);
  assert.equal(isLoopbackHost("localhost:63387"), true);
  assert.equal(isLoopbackHost("127.0.0.1:63387"), true);
  assert.equal(isLoopbackHost("[::1]:63387"), true);
  assert.equal(isLoopbackHost("::1"), true);
  assert.equal(isLoopbackHost("127.0.0.1.evil.test"), false);
  assert.equal(isLoopbackHost("localhost.evil.test"), false);
  assert.equal(isLoopbackHost("localhost:bad-port"), false);
  assert.equal(isLoopbackHost("localhost:"), false);
  assert.equal(isLoopbackHost("localhost:63387.evil.test"), false);
  assert.equal(isLoopbackHost("localhost:63387:evil"), false);
  assert.equal(isLoopbackHost("[::1]:bad-port"), false);
  assert.equal(isLoopbackHost("[::1]extra"), false);
  assert.equal(isLoopbackHost("192.168.1.10:63387"), false);
});

test("ApiServer exposes health and agent creation routes", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-api-"));
  const store = new StateStore(root);
  await store.init();
  const logs = new LogStore(root);
  const runtime = new RuntimeManager(store, logs);
  const api = new ApiServer({
    store,
    logs,
    runtime,
    detector: testDetector(),
    core: new UnavailableCoreNative(),
    instanceId: "api-test-instance",
    allowUnauthenticatedManagement: true,
  });
  const server = await api.listen(0, "127.0.0.1");
  const baseUrl = urlFor(server);

  try {
    const readableRoot = path.join(root, "readable-root");
    const writableRoot = path.join(root, "writable-root");
    await fs.mkdir(readableRoot);
    await fs.mkdir(writableRoot);

    const health = await getJson(`${baseUrl}/health`);
    assert.equal(health.ok, true);
    assert.equal(health.instanceId, "api-test-instance");
    assert.equal(health.stateHome, root);

    const legacyInfo = await getJson(`${baseUrl}/v1/info`);
    assert.equal(legacyInfo.service, "puffo-agent-bridge");
    assert.equal(legacyInfo.runtime, "agent-core");
    assert.equal(legacyInfo.daemon_version, "0.1.0");
    assert.equal(legacyInfo.pid, process.pid);
    assert.equal(legacyInfo.agent_count, 0);
    assert.equal(legacyInfo.paired, false);
    assert.equal(legacyInfo.paired_slug, null);
    assert.equal(legacyInfo.paired_device_id, null);
    assert.equal(legacyInfo.hostname.length > 0, true);

    const legacyPair = await postJsonStatus(`${baseUrl}/v1/pair`, 404, {});
    assert.equal(legacyPair.error.code, "not_found");
    const legacySecretBundleAgent = await postJsonStatus(`${baseUrl}/v1/agents`, 404, {
      identity_bundle: {
        root_secret_key: "must-not-enter-daemon",
        device_signing_secret_key: "must-not-enter-daemon",
        kem_secret_key: "must-not-enter-daemon",
      },
    });
    assert.equal(legacySecretBundleAgent.error.code, "not_found");

    const pairing = await postJsonStatus(`${baseUrl}/pairing/start`, 200, {});
    assert.equal(pairing.status, "unavailable");
    assert.deepEqual(pairing.blockedBy, ["native_core_unavailable"]);
    const confirmed = await postJsonStatus(`${baseUrl}/pairing/confirm`, 200, {
      authToken: "placeholder-server-token",
    });
    assert.equal(confirmed.status, "unavailable");
    assert.deepEqual(confirmed.blockedBy, ["native_core_unavailable"]);

    const created = await postJson(`${baseUrl}/agents`, {
      name: "Local Agent",
      provider: "codex",
      accessMode: "safe",
      networkAccess: "deny",
      deniedTools: ["python"],
      fileAccess: {
        readablePaths: [readableRoot],
        writablePaths: [writableRoot],
      },
      providerConfigPaths: [".codex/prompts"],
    });
    assert.equal(created.name, "Local Agent");
    assert.equal(created.provider, "codex");
    assert.equal(created.networkAccess, "deny");
    assert.deepEqual(created.deniedTools, ["python"]);
    assert.deepEqual(created.fileAccess, {
      readablePaths: [await fs.realpath(readableRoot)],
      writablePaths: [await fs.realpath(writableRoot)],
    });
    assert.deepEqual(created.providerConfigPaths, [".codex/prompts"]);

    const listed = await getJson(`${baseUrl}/agents`);
    assert.equal(listed.agents.length, 1);
    assert.equal(listed.agents[0].id, created.id);
    const legacyInfoAfterCreate = await getJson(`${baseUrl}/v1/info`);
    assert.equal(legacyInfoAfterCreate.agent_count, 1);

    const diagnostics = await getJson(`${baseUrl}/diagnostics`);
    assert.equal(diagnostics.core.status, "unavailable");
    assert.deepEqual(diagnostics.core.blockedBy, ["native_core_unavailable"]);
    assert.equal(diagnostics.agents.length, 1);

    const detail = await getJson(`${baseUrl}/agents/${created.id}`);
    assert.equal(detail.id, created.id);
    assert.equal(detail.name, "Local Agent");
    const missing = await getStatus(`${baseUrl}/agents/missing-agent`);
    assert.equal(missing.status, 404);
    const missingStatus = await getStatus(`${baseUrl}/agents/missing-agent/status`);
    assert.equal(missingStatus.status, 404);
    assert.equal((await missingStatus.json()).error.code, "not_found");
    const missingStart = await postJsonStatus(`${baseUrl}/agents/missing-agent/start`, 404, {});
    assert.equal(missingStart.error.code, "not_found");
    const missingRecheck = await postJsonStatus(`${baseUrl}/agents/missing-agent/recheck`, 404, {});
    assert.equal(missingRecheck.error.code, "not_found");
    const missingLogs = await getStatus(`${baseUrl}/agents/missing-agent/logs`);
    assert.equal(missingLogs.status, 404);
    assert.equal((await missingLogs.json()).error.code, "not_found");
    const invalidLogPath = await getStatus(`${baseUrl}/agents/invalid.id/logs`);
    assert.equal(invalidLogPath.status, 404);
    const tooLongAgentId = "a".repeat(129);
    const tooLongAgentPath = await getStatus(`${baseUrl}/agents/${tooLongAgentId}`);
    assert.equal(tooLongAgentPath.status, 404);

    const status = await getJson(`${baseUrl}/agents/${created.id}/status`);
    assert.equal(status.agent.id, created.id);
    assert.equal(status.runtime.attached, false);

    const startWithoutIdentity = await postJsonStatus(`${baseUrl}/agents/${created.id}/start`, 400, {});
    assert.equal(startWithoutIdentity.error.code, "bad_request");
    assert.match(startWithoutIdentity.error.message, /operatorSlug/);
    const restartWithoutIdentity = await postJsonStatus(`${baseUrl}/agents/${created.id}/restart`, 400, {});
    assert.equal(restartWithoutIdentity.error.code, "bad_request");
    assert.match(restartWithoutIdentity.error.message, /operatorSlug/);

    const recheck = await postJsonStatus(`${baseUrl}/agents/${created.id}/recheck`, 200, {});
    assert.equal(recheck.provider.provider, "codex");
    assert.equal(recheck.provider.installed, false);
    assert.equal(recheck.provider.reason, "not_found");

    const stopWithBody = await postJsonStatus(`${baseUrl}/agents/${created.id}/stop`, 400, {
      reason: "user-requested",
      token: "must-not-be-accepted",
    });
    assert.equal(stopWithBody.error.code, "bad_request");
    assert.match(stopWithBody.error.message, /body must be empty/);
    const stopWithArrayBody = await postJsonStatus(`${baseUrl}/agents/${created.id}/stop`, 400, []);
    assert.equal(stopWithArrayBody.error.code, "bad_request");
    assert.match(stopWithArrayBody.error.message, /JSON object/);

    const previewPolicy = await postJsonStatus(`${baseUrl}/agents/${created.id}/policy?preview=true`, 200, {
      accessMode: "project",
      projectPath: root,
      networkAccess: "inherit",
      deniedTools: ["node"],
      fileAccess: {
        readablePaths: [root],
      },
      providerConfigPaths: [".codex/skills"],
    });
    assert.equal(previewPolicy.agent.id, created.id);
    assert.equal(previewPolicy.agent.accessMode, "project");
    assert.equal(previewPolicy.policy.accessMode, "project");
    assert.equal(previewPolicy.policy.cwd, await fs.realpath(root));
    assert.equal(previewPolicy.policy.projectPath, await fs.realpath(root));
    assert.deepEqual(previewPolicy.policy.fileAccess, {
      readablePaths: [await fs.realpath(root)],
      writablePaths: [await fs.realpath(writableRoot)],
    });
    assert.deepEqual(previewPolicy.policy.providerConfigPaths, [".codex/skills"]);
    assert.equal(previewPolicy.policy.env, undefined);

    const detailAfterPreview = await getJson(`${baseUrl}/agents/${created.id}`);
    assert.equal(detailAfterPreview.accessMode, "safe");
    assert.equal(detailAfterPreview.projectPath, undefined);
    assert.equal(detailAfterPreview.networkAccess, "deny");
    assert.deepEqual(detailAfterPreview.deniedTools, ["python"]);
    assert.deepEqual(detailAfterPreview.fileAccess, {
      readablePaths: [await fs.realpath(readableRoot)],
      writablePaths: [await fs.realpath(writableRoot)],
    });
    assert.deepEqual(detailAfterPreview.providerConfigPaths, [".codex/prompts"]);

    const invalidPreviewFlag = await postJsonStatus(`${baseUrl}/agents/${created.id}/policy?preview=maybe`, 400, {});
    assert.equal(invalidPreviewFlag.error.code, "bad_request");
    assert.match(invalidPreviewFlag.error.message, /preview/);
    const duplicatePreviewFlag = await postJsonStatus(
      `${baseUrl}/agents/${created.id}/policy?preview=true&preview=false`,
      400,
      {},
    );
    assert.equal(duplicatePreviewFlag.error.code, "bad_request");
    assert.match(duplicatePreviewFlag.error.message, /preview/);
    const unsupportedPolicyQuery = await postJsonStatus(
      `${baseUrl}/agents/${created.id}/policy?dryRun=true`,
      400,
      {},
    );
    assert.equal(unsupportedPolicyQuery.error.code, "bad_request");
    assert.match(unsupportedPolicyQuery.error.message, /dryRun/);

    const updatedPolicy = await postJsonStatus(`${baseUrl}/agents/${created.id}/policy`, 200, {
      accessMode: "project",
      projectPath: root,
      networkAccess: "inherit",
      deniedTools: ["node"],
    });
    assert.equal(updatedPolicy.accessMode, "project");
    assert.equal(updatedPolicy.projectPath, await fs.realpath(root));
    assert.equal(updatedPolicy.networkAccess, "inherit");
    assert.deepEqual(updatedPolicy.deniedTools, ["node"]);

    const policyPreview = await getJson(`${baseUrl}/agents/${created.id}/policy`);
    assert.equal(policyPreview.agent.id, created.id);
    assert.equal(policyPreview.policy.accessMode, "project");
    assert.equal(policyPreview.policy.cwd, await fs.realpath(root));
    assert.equal(policyPreview.policy.projectPath, await fs.realpath(root));
    assert.equal(policyPreview.policy.networkAccess, "inherit");
    assert.deepEqual(policyPreview.policy.deniedTools, ["node"]);
    assert.equal(policyPreview.policy.env, undefined);

    const invalidPolicy = await postJsonStatus(`${baseUrl}/agents/${created.id}/policy`, 400, {
      accessMode: "project",
      projectPath: null,
    });
    assert.equal(invalidPolicy.error.code, "bad_request");
    assert.match(invalidPolicy.error.message, /projectPath is required/);

    const unsupportedPolicyField = await postJsonStatus(`${baseUrl}/agents/${created.id}/policy`, 400, {
      accessMode: "safe",
      apiKey: "must-not-enter-policy",
    });
    assert.equal(unsupportedPolicyField.error.code, "bad_request");
    assert.match(unsupportedPolicyField.error.message, /apiKey/);

    const trustedPolicyWithoutClearingRestrictions = await postJsonStatus(`${baseUrl}/agents/${created.id}/policy`, 400, {
      accessMode: "trusted",
    });
    assert.equal(trustedPolicyWithoutClearingRestrictions.error.code, "bad_request");
    assert.match(trustedPolicyWithoutClearingRestrictions.error.message, /safe or project/);

    const trustedPolicy = await postJsonStatus(`${baseUrl}/agents/${created.id}/policy`, 200, {
      accessMode: "trusted",
      networkAccess: "inherit",
      deniedTools: [],
      fileAccess: null,
      providerConfigPaths: null,
      projectPath: null,
    });
    assert.equal(trustedPolicy.accessMode, "trusted");
    assert.equal(trustedPolicy.networkAccess, "inherit");
    assert.deepEqual(trustedPolicy.deniedTools, []);
    assert.deepEqual(trustedPolicy.fileAccess, { readablePaths: [], writablePaths: [] });
    assert.deepEqual(trustedPolicy.providerConfigPaths, []);
    assert.equal(trustedPolicy.projectPath, undefined);

    const devInject = await postJsonStatus(`${baseUrl}/agents/${created.id}/dev-inject`, 404, {
      body: "blocked",
    });
    assert.equal(devInject.error.code, "not_found");

    const deleteWithBody = await deleteJsonStatus(
      `${baseUrl}/agents/${created.id}`,
      400,
      undefined,
      { reason: "user-requested", token: "must-not-be-accepted" },
    );
    assert.equal(deleteWithBody.error.code, "bad_request");
    assert.match(deleteWithBody.error.message, /body must be empty/);

    const deleted = await deleteJsonStatus(`${baseUrl}/agents/${created.id}`, 200);
    assert.equal(deleted.deleted, true);
    assert.equal(deleted.id, created.id);
    const deletedDetail = await getStatus(`${baseUrl}/agents/${created.id}`);
    assert.equal(deletedDetail.status, 404);

    const invalidProject = await postJsonStatus(`${baseUrl}/agents`, 400, {
      name: "Broken Project Agent",
      provider: "codex",
      accessMode: "project",
    });
    assert.equal(invalidProject.error.code, "bad_request");
    assert.match(invalidProject.error.message, /projectPath is required/);

    const missingProjectDir = await postJsonStatus(`${baseUrl}/agents`, 400, {
      name: "Missing Project Dir",
      provider: "codex",
      accessMode: "project",
      projectPath: path.join(root, "does-not-exist"),
    });
    assert.equal(missingProjectDir.error.code, "bad_request");
    assert.match(missingProjectDir.error.message, /existing directory/);

    const relativeTrustedProject = await postJsonStatus(`${baseUrl}/agents`, 400, {
      name: "Relative Trusted Project",
      provider: "codex",
      accessMode: "trusted",
      projectPath: "relative/path",
    });
    assert.equal(relativeTrustedProject.error.code, "bad_request");
    assert.match(relativeTrustedProject.error.message, /absolute/);

    const trustedDeniedNetwork = await postJsonStatus(`${baseUrl}/agents`, 400, {
      name: "Trusted Denied Network",
      provider: "codex",
      accessMode: "trusted",
      networkAccess: "deny",
    });
    assert.equal(trustedDeniedNetwork.error.code, "bad_request");
    assert.match(trustedDeniedNetwork.error.message, /safe or project/);

    const trustedDeniedTools = await postJsonStatus(`${baseUrl}/agents`, 400, {
      name: "Trusted Denied Tools",
      provider: "codex",
      accessMode: "trusted",
      deniedTools: ["python"],
    });
    assert.equal(trustedDeniedTools.error.code, "bad_request");
    assert.match(trustedDeniedTools.error.message, /safe or project/);

    const trustedFileAccess = await postJsonStatus(`${baseUrl}/agents`, 400, {
      name: "Trusted File Access",
      provider: "codex",
      accessMode: "trusted",
      fileAccess: { readablePaths: [root] },
    });
    assert.equal(trustedFileAccess.error.code, "bad_request");
    assert.match(trustedFileAccess.error.message, /safe or project/);

    const trustedProviderConfigPaths = await postJsonStatus(`${baseUrl}/agents`, 400, {
      name: "Trusted Provider Config",
      provider: "codex",
      accessMode: "trusted",
      providerConfigPaths: [".codex/prompts"],
    });
    assert.equal(trustedProviderConfigPaths.error.code, "bad_request");
    assert.match(trustedProviderConfigPaths.error.message, /safe or project/);

    const unsupportedCreateField = await postJsonStatus(`${baseUrl}/agents`, 400, {
      name: "Unexpected Field",
      provider: "codex",
      apiKey: "must-not-enter-state",
    });
    assert.equal(unsupportedCreateField.error.code, "bad_request");
    assert.match(unsupportedCreateField.error.message, /apiKey/);

    const unsupportedFileAccessField = await postJsonStatus(`${baseUrl}/agents`, 400, {
      name: "Unexpected File Access",
      provider: "codex",
      fileAccess: {
        readablePaths: [root],
        credentialPath: root,
      },
    });
    assert.equal(unsupportedFileAccessField.error.code, "bad_request");
    assert.match(unsupportedFileAccessField.error.message, /credentialPath/);

    const invalidSlug = await postJsonStatus(`${baseUrl}/agents`, 400, {
      name: "Broken Slug Agent",
      provider: "codex",
      operatorSlug: "Alice Smith",
    });
    assert.equal(invalidSlug.error.code, "bad_request");
    assert.match(invalidSlug.error.message, /operatorSlug/);

    const agentSlugWithoutOperator = await postJsonStatus(`${baseUrl}/agents`, 400, {
      name: "Broken Agent Slug Agent",
      provider: "codex",
      agentSlug: "alice-agent",
    });
    assert.equal(agentSlugWithoutOperator.error.code, "bad_request");
    assert.match(agentSlugWithoutOperator.error.message, /agentSlug requires operatorSlug/);

    const startWithoutOperator = await postJsonStatus(`${baseUrl}/agents`, 400, {
      name: "Broken Start Identity Agent",
      provider: "codex",
      start: true,
    });
    assert.equal(startWithoutOperator.error.code, "bad_request");
    assert.match(startWithoutOperator.error.message, /operatorSlug or coreIdentity is required/);

    const unavailableCoreIdentity = await postJsonStatus(`${baseUrl}/agents`, 400, {
      name: "Broken Core Identity Agent",
      provider: "codex",
      operatorSlug: "alice",
    });
    assert.equal(unavailableCoreIdentity.error.code, "bad_request");
    assert.match(unavailableCoreIdentity.error.message, /core identity creation is unavailable/);

    const webSignedIdentity = await postJson(`${baseUrl}/agents`, {
      name: "Web Signed Identity Agent",
      provider: "codex",
      agentSlug: "alice-agent",
      coreIdentity: {
        operatorSlug: "alice",
        agentSlug: "alice-agent",
        identityType: "agent",
        declaredOperatorPublicKey: "operator-pub",
      },
    });
    assert.equal(webSignedIdentity.coreIdentity.operatorSlug, "alice");
    assert.equal(webSignedIdentity.coreIdentity.agentSlug, "alice-agent");
    assert.equal(webSignedIdentity.coreIdentity.identityType, "agent");
    assert.equal(webSignedIdentity.coreIdentity.source, "web_signed");

    const coreIdentityMissingOperatorKey = await postJsonStatus(`${baseUrl}/agents`, 400, {
      name: "Incomplete Web Identity Agent",
      provider: "codex",
      coreIdentity: {
        operatorSlug: "alice",
        agentSlug: "alice-agent",
        identityType: "agent",
      },
    });
    assert.equal(coreIdentityMissingOperatorKey.error.code, "bad_request");
    assert.match(coreIdentityMissingOperatorKey.error.message, /declaredOperatorPublicKey/);

    const coreIdentityOversizedOperatorKey = await postJsonStatus(`${baseUrl}/agents`, 400, {
      name: "Oversized Operator Key Agent",
      provider: "codex",
      coreIdentity: {
        operatorSlug: "alice",
        agentSlug: "alice-agent",
        identityType: "agent",
        declaredOperatorPublicKey: "k".repeat(4097),
      },
    });
    assert.equal(coreIdentityOversizedOperatorKey.error.code, "bad_request");
    assert.match(coreIdentityOversizedOperatorKey.error.message, /4096/);

    const previewMissingOperatorKey = await postJsonStatus(`${baseUrl}/agents/preview`, 400, {
      name: "Incomplete Preview Identity Agent",
      provider: "codex",
      coreIdentity: {
        operatorSlug: "alice",
        agentSlug: "alice-agent",
        identityType: "agent",
      },
    });
    assert.equal(previewMissingOperatorKey.error.code, "bad_request");
    assert.match(previewMissingOperatorKey.error.message, /declaredOperatorPublicKey/);

    const coreIdentityNativeSource = await postJsonStatus(`${baseUrl}/agents`, 400, {
      name: "Misleading Source Agent",
      provider: "codex",
      coreIdentity: {
        operatorSlug: "alice",
        agentSlug: "alice-agent",
        identityType: "agent",
        declaredOperatorPublicKey: "operator-pub",
        source: "native",
      },
    });
    assert.equal(coreIdentityNativeSource.error.code, "bad_request");
    assert.match(coreIdentityNativeSource.error.message, /web_signed/);

    const previewNativeSource = await postJsonStatus(`${baseUrl}/agents/preview`, 400, {
      name: "Misleading Preview Source Agent",
      provider: "codex",
      coreIdentity: {
        operatorSlug: "alice",
        agentSlug: "alice-agent",
        identityType: "agent",
        declaredOperatorPublicKey: "operator-pub",
        source: "native",
      },
    });
    assert.equal(previewNativeSource.error.code, "bad_request");
    assert.match(previewNativeSource.error.message, /web_signed/);

    const coreIdentitySecretField = await postJsonStatus(`${baseUrl}/agents`, 400, {
      name: "Leaky Web Identity Agent",
      provider: "codex",
      coreIdentity: {
        operatorSlug: "alice",
        agentSlug: "alice-agent",
        identityType: "agent",
        declaredOperatorPublicKey: "operator-pub",
        root_secret_key: "must-not-enter-daemon",
      },
    });
    assert.equal(coreIdentitySecretField.error.code, "bad_request");
    assert.match(coreIdentitySecretField.error.message, /root_secret_key/);

    const legacyIdentityBundle = await postJsonStatus(`${baseUrl}/agents`, 400, {
      name: "Legacy Secret Bundle Agent",
      provider: "codex",
      identity_bundle: {
        root_secret_key: "must-not-enter-daemon",
        device_signing_secret_key: "must-not-enter-daemon",
        kem_secret_key: "must-not-enter-daemon",
      },
    });
    assert.equal(legacyIdentityBundle.error.code, "bad_request");
    assert.match(legacyIdentityBundle.error.message, /identity_bundle/);

    const invalidNetwork = await postJsonStatus(`${baseUrl}/agents`, 400, {
      name: "Broken Network Agent",
      provider: "codex",
      networkAccess: "blocked",
    });
    assert.equal(invalidNetwork.error.code, "bad_request");
    assert.match(invalidNetwork.error.message, /networkAccess/);

    const invalidInstructions = await postJsonStatus(`${baseUrl}/agents`, 400, {
      name: "Broken Instructions Agent",
      provider: "codex",
      instructions: { text: "hello" },
    });
    assert.equal(invalidInstructions.error.code, "bad_request");
    assert.match(invalidInstructions.error.message, /instructions/);

    const invalidStart = await postJsonStatus(`${baseUrl}/agents`, 400, {
      name: "Broken Start Agent",
      provider: "codex",
      start: "false",
    });
    assert.equal(invalidStart.error.code, "bad_request");
    assert.match(invalidStart.error.message, /start/);

    const invalidDeniedTools = await postJsonStatus(`${baseUrl}/agents`, 400, {
      name: "Broken Tools Agent",
      provider: "codex",
      deniedTools: ["python", ""],
    });
    assert.equal(invalidDeniedTools.error.code, "bad_request");
    assert.match(invalidDeniedTools.error.message, /deniedTools/);

    const invalidFileAccess = await postJsonStatus(`${baseUrl}/agents`, 400, {
      name: "Broken File Access Agent",
      provider: "codex",
      fileAccess: { readablePaths: ["relative/path"] },
    });
    assert.equal(invalidFileAccess.error.code, "bad_request");
    assert.match(invalidFileAccess.error.message, /absolute/);

    const invalidProviderConfigPaths = await postJsonStatus(`${baseUrl}/agents`, 400, {
      name: "Broken Provider Config Agent",
      provider: "codex",
      providerConfigPaths: ["../.ssh"],
    });
    assert.equal(invalidProviderConfigPaths.error.code, "bad_request");
    assert.match(invalidProviderConfigPaths.error.message, /supported codex/);

    const sensitiveProviderConfigPaths = await postJsonStatus(`${baseUrl}/agents`, 400, {
      name: "Sensitive Provider Config Agent",
      provider: "codex",
      providerConfigPaths: [".ssh/config"],
    });
    assert.equal(sensitiveProviderConfigPaths.error.code, "bad_request");
    assert.match(sensitiveProviderConfigPaths.error.message, /supported codex/);

    const crossProviderConfigPaths = await postJsonStatus(`${baseUrl}/agents`, 400, {
      name: "Cross Provider Config Agent",
      provider: "codex",
      providerConfigPaths: [".claude/commands"],
    });
    assert.equal(crossProviderConfigPaths.error.code, "bad_request");
    assert.match(crossProviderConfigPaths.error.message, /supported codex/);

    const broadProviderConfigPaths = await postJsonStatus(`${baseUrl}/agents`, 400, {
      name: "Broad Provider Config Agent",
      provider: "codex",
      providerConfigPaths: [".codex"],
    });
    assert.equal(broadProviderConfigPaths.error.code, "bad_request");
    assert.match(broadProviderConfigPaths.error.message, /supported codex/);

    const badJson = await postRawStatus(`${baseUrl}/agents`, 400, "{not-json");
    assert.equal(badJson.error.code, "bad_request");
    assert.match(badJson.error.message, /invalid JSON/);

    const wrongContentType = await postRawStatus(`${baseUrl}/agents`, 415, "{}", undefined, "text/plain");
    assert.equal(wrongContentType.error.code, "unsupported_media_type");
    assert.match(wrongContentType.error.message, /application\/json/);

    const tooLarge = await postRawStatus(
      `${baseUrl}/agents`,
      413,
      JSON.stringify({ name: "x".repeat(1024 * 1024), provider: "codex" }),
    );
    assert.equal(tooLarge.error.code, "payload_too_large");
  } finally {
    await api.close();
  }
});

test("ApiServer canonicalizes project paths before persisting policy", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-api-project-"));
  const project = path.join(root, "project");
  const link = path.join(root, "project-link");
  await fs.mkdir(project);
  await fs.symlink(project, link);
  const store = new StateStore(root);
  await store.init();
  const logs = new LogStore(root);
  const runtime = new RuntimeManager(store, logs);
  const api = new ApiServer({
    store,
    logs,
    runtime,
    detector: testDetector(),
    core: new UnavailableCoreNative(),
    allowUnauthenticatedManagement: true,
  });
  const server = await api.listen(0, "127.0.0.1");
  const baseUrl = urlFor(server);

  try {
    const created = await postJson(`${baseUrl}/agents`, {
      name: "Project Agent",
      provider: "codex",
      accessMode: "project",
      projectPath: link,
    });
    assert.equal(created.projectPath, await fs.realpath(project));
  } finally {
    await api.close();
  }
});

test("ApiServer refuses core identity creation when native core is not ready", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-api-identity-not-ready-"));
  const store = new StateStore(root);
  await store.init();
  const logs = new LogStore(root);
  const core = new PairingRequiredIdentityCoreNative();
  const runtime = new RuntimeManager(store, logs, core);
  const api = new ApiServer({
    store,
    logs,
    runtime,
    detector: testDetector(),
    core,
    allowUnauthenticatedManagement: true,
  });
  const server = await api.listen(0, "127.0.0.1");
  const baseUrl = urlFor(server);

  try {
    const rejected = await postJsonStatus(`${baseUrl}/agents`, 400, {
      name: "Prod Identity Agent",
      provider: "codex",
      operatorSlug: "alice",
    });

    assert.equal(rejected.error.code, "bad_request");
    assert.match(rejected.error.message, /native core ready/);
    const listed = await getJson(`${baseUrl}/agents`);
    assert.deepEqual(listed.agents, []);

    const bypassed = await postJson(`${baseUrl}/agents`, {
      name: "Web Signed Prod Identity Agent",
      provider: "codex",
      coreIdentity: {
        operatorSlug: "alice",
        agentSlug: "alice-agent",
        identityType: "agent",
        declaredOperatorPublicKey: "operator-pub",
        source: "web_signed",
      },
    });
    assert.equal(bypassed.coreIdentity.agentSlug, "alice-agent");
  } finally {
    await api.close();
  }
});

test("ApiServer previews agent creation policy without persisting", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-api-create-preview-"));
  const project = path.join(root, "project");
  const readable = path.join(root, "readable");
  await fs.mkdir(project);
  await fs.mkdir(readable);
  const store = new StateStore(root);
  await store.init();
  const logs = new LogStore(root);
  const runtime = new RuntimeManager(store, logs);
  const api = new ApiServer({
    store,
    logs,
    runtime,
    detector: testDetector(),
    core: new UnavailableCoreNative(),
    allowUnauthenticatedManagement: true,
  });
  const server = await api.listen(0, "127.0.0.1");
  const baseUrl = urlFor(server);

  try {
    const preview = await postJsonStatus(`${baseUrl}/agents/preview`, 200, {
      name: "Preview Agent",
      provider: "codex",
      accessMode: "project",
      projectPath: project,
      networkAccess: "deny",
      deniedTools: ["security"],
      fileAccess: { readablePaths: [readable] },
      providerConfigPaths: [".codex/skills"],
      operatorSlug: "alice",
      start: true,
    });
    assert.equal(preview.agent.name, "Preview Agent");
    assert.equal(preview.agent.status, "created");
    assert.equal(preview.agent.projectPath, await fs.realpath(project));
    assert.equal(preview.policy.accessMode, "project");
    assert.equal(preview.policy.projectPath, await fs.realpath(project));
    assert.equal(preview.policy.networkAccess, "deny");
    assert.deepEqual(preview.policy.deniedTools, ["security"]);
    assert.deepEqual(preview.policy.fileAccess, {
      readablePaths: [await fs.realpath(readable)],
      writablePaths: [],
    });
    assert.deepEqual(preview.policy.providerConfigPaths, [".codex/skills"]);
    assert.equal(preview.policy.env, undefined);

    const listed = await getJson(`${baseUrl}/agents`);
    assert.deepEqual(listed.agents, []);
  } finally {
    await api.close();
  }
});

test("ApiServer refuses policy updates that would restart a running agent without core identity", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-api-policy-running-no-identity-"));
  const store = new StateStore(root);
  await store.init();
  const logs = new LogStore(root);
  const runtime = new RuntimeManager(store, logs, undefined, {
    providerFactory: () => new FakeProviderSession(),
  });
  const agent = await runtime.createAgent({
    name: "Legacy Running Agent",
    provider: "codex",
  });
  await runtime.startAgent(agent.id);
  const api = new ApiServer({
    store,
    logs,
    runtime,
    detector: testDetector(),
    core: new UnavailableCoreNative(),
    allowUnauthenticatedManagement: true,
  });
  const server = await api.listen(0, "127.0.0.1");
  const baseUrl = urlFor(server);

  try {
    const preview = await postJsonStatus(`${baseUrl}/agents/${agent.id}/policy?preview=true`, 200, {
      networkAccess: "deny",
    });
    assert.equal(preview.agent.networkAccess, "deny");

    const rejected = await postJsonStatus(`${baseUrl}/agents/${agent.id}/policy`, 400, {
      networkAccess: "deny",
    });
    assert.equal(rejected.error.code, "bad_request");
    assert.match(rejected.error.message, /operatorSlug/);

    const saved = await store.getAgent(agent.id);
    assert.equal(saved?.status, "running");
    assert.notEqual(saved?.networkAccess, "deny");
  } finally {
    await runtime.stopAgent(agent.id).catch(() => undefined);
    await api.close();
  }
});

test("ApiServer reports stale project policy previews as bad requests", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-api-stale-project-"));
  const project = path.join(root, "project");
  await fs.mkdir(project);
  const store = new StateStore(root);
  await store.init();
  const logs = new LogStore(root);
  const runtime = new RuntimeManager(store, logs);
  const api = new ApiServer({
    store,
    logs,
    runtime,
    detector: testDetector(),
    core: new UnavailableCoreNative(),
    allowUnauthenticatedManagement: true,
  });
  const server = await api.listen(0, "127.0.0.1");
  const baseUrl = urlFor(server);

  try {
    const created = await postJson(`${baseUrl}/agents`, {
      name: "Stale Project Agent",
      provider: "codex",
      accessMode: "project",
      projectPath: project,
    });
    await fs.rm(project, { recursive: true, force: true });

    const response = await getStatus(`${baseUrl}/agents/${created.id}/policy`);
    assert.equal(response.status, 400);
    const body = await response.json();
    assert.equal(body.error.code, "bad_request");
    assert.match(body.error.message, /projectPath/);
  } finally {
    await api.close();
  }
});

test("ApiServer listen rejects cleanly when the port is unavailable", async () => {
  const occupied = http.createServer((_req, res) => res.end("occupied"));
  await new Promise<void>((resolve) => occupied.listen(0, "127.0.0.1", resolve));
  const address = occupied.address();
  assert(address && typeof address === "object");

  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-api-port-"));
  const store = new StateStore(root);
  await store.init();
  const api = new ApiServer({
    store,
    logs: new LogStore(root),
    runtime: new RuntimeManager(store, new LogStore(root)),
    detector: testDetector(),
    core: new UnavailableCoreNative(),
  });

  try {
    await assert.rejects(api.listen(address.port, "127.0.0.1"), /EADDRINUSE/);
  } finally {
    await new Promise<void>((resolve, reject) => occupied.close((error) => (error ? reject(error) : resolve())));
    await api.close();
  }
});

test("ApiServer redacts internal error messages", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-api-redact-"));
  const store = new StateStore(root);
  await store.init();
  const api = new ApiServer({
    store,
    logs: new LogStore(root),
    runtime: new RuntimeManager(store, new LogStore(root)),
    detector: testDetector(),
    core: new SecretThrowingCoreNative(),
    allowUnauthenticatedManagement: true,
  });
  const server = await api.listen(0, "127.0.0.1");
  const baseUrl = urlFor(server);

  try {
    const failed = await postJsonStatus(`${baseUrl}/pairing/start`, 500, {});
    assert.equal(failed.error.code, "internal_error");
    assert.doesNotMatch(failed.error.message, /secret-value/);
    assert.match(failed.error.message, /token=\[redacted\]/);

    const diagnostics = await getJson(`${baseUrl}/diagnostics`);
    assert.doesNotMatch(diagnostics.core.reason, /secret-value/);
    assert.match(diagnostics.core.reason, /token=\[redacted\]/);
    assert.deepEqual(diagnostics.core.blockedBy, ["native_core_error"]);
    assert.match(diagnostics.core.nextAction, /agent doctor/);
  } finally {
    await api.close();
  }
});

test("ApiServer requires control token for management routes when configured", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-api-auth-"));
  const store = new StateStore(root);
  await store.init();
  const logs = new LogStore(root);
  const runtime = new RuntimeManager(store, logs);
  const api = new ApiServer({
    store,
    logs,
    runtime,
    detector: testDetector(),
    core: new UnavailableCoreNative(),
    apiToken: "local-token",
  });
  const server = await api.listen(0, "127.0.0.1");
  const baseUrl = urlFor(server);

  try {
    const health = await getJson(`${baseUrl}/health`);
    assert.equal(health.authRequired, true);
    assert.equal(health.stateHome, undefined);

    const authedHealth = await getJson(`${baseUrl}/health`, "local-token");
    assert.equal(authedHealth.stateHome, root);

    const unauthorized = await fetch(`${baseUrl}/agents`);
    assert.equal(unauthorized.status, 401);

    const legacySignedBridgeAuth = await fetch(`${baseUrl}/agents`, {
      headers: {
        "X-Puffo-Slug": "alice",
        "X-Puffo-Signer-Id": "device-old",
        "X-Puffo-Timestamp": String(Date.now()),
        "X-Puffo-Nonce": "legacy-nonce",
        "X-Puffo-Signature": "legacy-signature",
      },
    });
    assert.equal(legacySignedBridgeAuth.status, 401);

    const preflight = await fetch(`${baseUrl}/agents`, {
      method: "OPTIONS",
      headers: {
        Origin: "https://app.example.test",
        "Access-Control-Request-Headers": "X-Agent-Core-Token",
        "Access-Control-Request-Private-Network": "true",
      },
    });
    assert.equal(preflight.status, 204);
    assert.equal(preflight.headers.get("access-control-allow-origin"), "*");
    assert.match(preflight.headers.get("access-control-allow-methods") ?? "", /DELETE/);
    assert.match(preflight.headers.get("access-control-allow-headers") ?? "", /X-Agent-Core-Token/);
    assert.equal(preflight.headers.get("access-control-allow-private-network"), "true");
    assert.equal(preflight.headers.get("cache-control"), "no-store");
    assert.equal(preflight.headers.get("x-content-type-options"), "nosniff");

    const listed = await getJson(`${baseUrl}/agents`, "local-token");
    assert.deepEqual(listed.agents, []);

    const created = await postJsonStatus(
      `${baseUrl}/agents`,
      201,
      {
        name: "Authed Agent",
        provider: "codex",
      },
      "local-token",
    );
    assert.equal(created.name, "Authed Agent");
  } finally {
    await api.close();
  }
});

test("ApiServer accepts a valid local token from any supported auth header", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-api-auth-headers-"));
  const store = new StateStore(root);
  await store.init();
  const grant = await store.createLocalAccessGrant({ ttlMs: 60_000 });
  const logs = new LogStore(root);
  const runtime = new RuntimeManager(store, logs);
  const api = new ApiServer({
    store,
    logs,
    runtime,
    detector: testDetector(),
    core: new UnavailableCoreNative(),
    apiToken: "local-token",
  });
  const server = await api.listen(0, "127.0.0.1");
  const baseUrl = urlFor(server);

  try {
    const controlViaFallbackHeader = await fetch(`${baseUrl}/agents`, {
      headers: {
        Authorization: "Bearer wrong-token",
        "X-Agent-Core-Token": "local-token",
      },
    });
    assert.equal(controlViaFallbackHeader.status, 200);
    assert.deepEqual((await controlViaFallbackHeader.json()).agents, []);

    const grantViaFallbackHeader = await fetch(`${baseUrl}/agents`, {
      headers: {
        Authorization: "Bearer wrong-token",
        "X-Agent-Core-Token": grant.token,
      },
    });
    assert.equal(grantViaFallbackHeader.status, 200);
    assert.deepEqual((await grantViaFallbackHeader.json()).agents, []);

    const minted = await fetch(`${baseUrl}/local-grants`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: "Bearer wrong-token",
        "X-Agent-Core-Token": "local-token",
      },
      body: "{}",
    });
    assert.equal(minted.status, 201);
    assert.match((await minted.json()).token, /^[A-Za-z0-9_-]+$/);

    const grantCannotMint = await fetch(`${baseUrl}/local-grants`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: "Bearer wrong-token",
        "X-Agent-Core-Token": grant.token,
      },
      body: "{}",
    });
    assert.equal(grantCannotMint.status, 401);
    assert.equal((await grantCannotMint.json()).error.code, "unauthorized");

    const invalid = await fetch(`${baseUrl}/agents`, {
      headers: {
        Authorization: "Bearer wrong-token",
        "X-Agent-Core-Token": "also-wrong",
      },
    });
    assert.equal(invalid.status, 401);
  } finally {
    await api.close();
  }
});

test("ApiServer forwards pairing auth token without echoing it", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-api-pairing-auth-"));
  const store = new StateStore(root);
  await store.init();
  const core = new RecordingPairingCoreNative();
  const api = new ApiServer({
    store,
    logs: new LogStore(root),
    runtime: new RuntimeManager(store, new LogStore(root)),
    detector: testDetector(),
    core,
    apiToken: "local-token",
  });
  const server = await api.listen(0, "127.0.0.1");
  const baseUrl = urlFor(server);

  try {
    const start = await postJsonStatus(`${baseUrl}/pairing/start`, 200, {});
    assert.deepEqual(core.startInput, {});
    assert.equal(start.status, "unavailable");

    core.startInput = undefined;
    const invalidStartBody = await postJsonStatus(`${baseUrl}/pairing/start`, 400, []);
    assert.equal(invalidStartBody.error.code, "bad_request");
    assert.match(invalidStartBody.error.message, /pairing start/);
    assert.equal(core.startInput, undefined);

    const unsupportedStartField = await postJsonStatus(
      `${baseUrl}/pairing/start`,
      400,
      { client: "web" },
    );
    assert.equal(unsupportedStartField.error.code, "bad_request");
    assert.match(unsupportedStartField.error.message, /client/);
    assert.equal(core.startInput, undefined);

    const secret = "server-issued-secret-value";
    const unauthenticatedConfirm = await postJsonStatus(
      `${baseUrl}/pairing/confirm`,
      401,
      { authToken: secret, pairingId: "pairing-1" },
    );
    assert.equal(unauthenticatedConfirm.error.code, "unauthorized");
    assert.equal(Boolean(core.confirmInput), false);

    const response = await postJsonStatus(
      `${baseUrl}/pairing/confirm`,
      200,
      { authToken: ` ${secret} `, pairingId: "pairing-1" },
      "local-token",
    );
    assert.equal(core.confirmInput?.authToken, secret);
    assert.equal(core.confirmInput?.pairingId, "pairing-1");
    assert.equal(response.status, "pairing_required");
    assert.equal(response.authTokenSource, "memory");
    assert.doesNotMatch(JSON.stringify(response), /server-issued-secret-value/);

    core.confirmInput = undefined;
    const missingAuthToken = await postJsonStatus(
      `${baseUrl}/pairing/confirm`,
      400,
      { pairingId: "pairing-2" },
      "local-token",
    );
    assert.equal(missingAuthToken.error.code, "bad_request");
    assert.match(missingAuthToken.error.message, /authToken/);
    assert.equal(core.confirmInput, undefined);

    const invalidAuthToken = await postJsonStatus(
      `${baseUrl}/pairing/confirm`,
      400,
      { authToken: 123 },
      "local-token",
    );
    assert.equal(invalidAuthToken.error.code, "bad_request");
    assert.match(invalidAuthToken.error.message, /authToken/);
    assert.equal(core.confirmInput, undefined);

    const oversizedAuthToken = await postJsonStatus(
      `${baseUrl}/pairing/confirm`,
      400,
      { authToken: "a".repeat(16 * 1024 + 1) },
      "local-token",
    );
    assert.equal(oversizedAuthToken.error.code, "bad_request");
    assert.match(oversizedAuthToken.error.message, /authToken/);
    assert.equal(core.confirmInput, undefined);

    const invalidPairingId = await postJsonStatus(
      `${baseUrl}/pairing/confirm`,
      400,
      { authToken: "server-token", pairingId: 123 },
      "local-token",
    );
    assert.equal(invalidPairingId.error.code, "bad_request");
    assert.match(invalidPairingId.error.message, /pairingId/);
    assert.equal(core.confirmInput, undefined);

    const unsafePairingId = await postJsonStatus(
      `${baseUrl}/pairing/confirm`,
      400,
      { authToken: "server-token", pairingId: "../pairing-1" },
      "local-token",
    );
    assert.equal(unsafePairingId.error.code, "bad_request");
    assert.match(unsafePairingId.error.message, /pairingId/);
    assert.equal(core.confirmInput, undefined);

    const unsupportedConfirmField = await postJsonStatus(
      `${baseUrl}/pairing/confirm`,
      400,
      {
        authToken: "server-token",
        operatorBootstrap: { kind: "restore_or_enroll", payload: { root_secret_key: "must-not-enter-native" } },
      },
      "local-token",
    );
    assert.equal(unsupportedConfirmField.error.code, "bad_request");
    assert.match(unsupportedConfirmField.error.message, /operatorBootstrap/);
    assert.equal(core.confirmInput, undefined);
  } finally {
    await api.close();
  }
});

test("ServerPairingGateway rejects malformed direct pairing inputs", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-server-pairing-boundary-"));
  const store = new StateStore(root);
  await store.init();
  const core = new RecordingPairingCoreNative();
  const gateway = new ServerPairingGateway(core, store, { serverUrl: "http://127.0.0.1:1" });

  await assert.rejects(gateway.startPairing([] as unknown as Record<string, unknown>), /pairing start/);
  await assert.rejects(gateway.startPairing({ client: "web" }), /client is not supported/);
  await assert.rejects(gateway.pollPairing("../pairing-1"), /pairingId must be a safe identifier/);
  await assert.rejects(
    gateway.confirmPairing({ authToken: "server-token", pairingId: "../pairing-1" }),
    /pairingId must be a safe identifier/,
  );
  await assert.rejects(
    gateway.confirmPairing({ authToken: "a".repeat(16 * 1024 + 1) }),
    /authToken must be a non-empty string/,
  );
  assert.equal(core.confirmInput, undefined);
});

test("ServerPairingGateway validates server URL before requests", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-server-pairing-url-"));
  const store = new StateStore(root);
  await store.init();
  const core = new RecordingPairingCoreNative();

  assert.doesNotThrow(() => new ServerPairingGateway(core, store, { serverUrl: "https://api.example.test/" }));
  assert.doesNotThrow(() => new ServerPairingGateway(core, store, { serverUrl: "http://127.0.0.1:63387/dev" }));
  assert.doesNotThrow(() => new ServerPairingGateway(core, store, { serverUrl: "http://localhost:63387" }));

  assert.throws(
    () => new ServerPairingGateway(core, store, { serverUrl: "http://api.example.test" }),
    /serverUrl must use HTTPS or loopback HTTP/,
  );
  assert.throws(
    () => new ServerPairingGateway(core, store, { serverUrl: "https://alice:secret@api.example.test" }),
    /serverUrl must not include credentials/,
  );
  assert.throws(
    () => new ServerPairingGateway(core, store, { serverUrl: "https://api.example.test?token=secret" }),
    /serverUrl must not include query or fragment/,
  );
  assert.throws(
    () => new ServerPairingGateway(core, store, { serverUrl: "file:///tmp/pairing" }),
    /serverUrl must use HTTPS or loopback HTTP/,
  );
});

test("ApiServer starts and polls server-confirmed pairings through the pairing gateway", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-api-server-pairing-"));
  const store = new StateStore(root);
  await store.init();
  const legacyGrant = await store.createLocalAccessGrant({
    ttlMs: 60_000,
    binding: { accountId: "acct_legacy", operatorSlug: "legacy" },
  });
  const core = new RecordingPairingCoreNative();
  const logs = new LogStore(root);
  const requests: unknown[] = [];
  let pollCount = 0;
  const backend = http.createServer(async (req, res) => {
    try {
      if (req.method === "POST" && req.url === "/agent-core/pairings") {
        requests.push(await readRequestJson(req));
        return sendTestJson(res, 201, {
          pairingId: "pair_1",
          userCode: "ABCD-EFGH",
          confirmUrl: "https://app.example.test/agents/pair?pairingId=pair_1",
          expiresAt: "2026-05-06T19:00:00.000Z",
          pollAfterMs: 1000,
        });
      }
      if (req.method === "GET" && req.url === "/agent-core/pairings/pair_1") {
        pollCount += 1;
        return sendTestJson(res, 200, {
          status: "confirmed",
          expiresAt: "2026-05-06T19:00:00.000Z",
          operatorSlug: "alice",
          accountId: "acct_alice",
          ...(pollCount === 1 ? { authToken: "agtok_server_secret" } : {}),
          operatorBootstrap: { kind: "existing_local_identity", operatorSlug: "alice" },
          localWebGrant: { mode: "daemon_mints", ttlMs: 60_000 },
        });
      }
      sendTestJson(res, 404, { error: "not_found" });
    } catch (error) {
      sendTestJson(res, 500, { error: String(error) });
    }
  });
  await listenTestServer(backend);

  const api = new ApiServer({
    store,
    logs,
    runtime: new RuntimeManager(store, logs),
    detector: testDetector(),
    core,
    pairing: new ServerPairingGateway(core, store, { serverUrl: urlFor(backend) }),
    apiToken: "local-token",
  });
  const server = await api.listen(0, "127.0.0.1");
  const baseUrl = urlFor(server);

  try {
    const spoofedOrigin = await postJsonStatus(
      `${baseUrl}/pairing/start`,
      400,
      { localApiOrigin: "https://evil.example.test" },
    );
    assert.equal(spoofedOrigin.error.code, "bad_request");
    assert.match(spoofedOrigin.error.message, /localApiOrigin/);
    assert.equal(requests.length, 0);

    const unsupportedStartField = await postJsonStatus(
      `${baseUrl}/pairing/start`,
      400,
      { authToken: "must-not-reach-backend" },
    );
    assert.equal(unsupportedStartField.error.code, "bad_request");
    assert.match(unsupportedStartField.error.message, /authToken/);
    assert.equal(requests.length, 0);

    const invalidNonce = await postJsonStatus(
      `${baseUrl}/pairing/start`,
      400,
      { pairingPublicNonce: "short nonce" },
    );
    assert.equal(invalidNonce.error.code, "bad_request");
    assert.match(invalidNonce.error.message, /pairingPublicNonce/);
    assert.equal(requests.length, 0);

    const untrustedBrowserStart = await fetch(`${baseUrl}/pairing/start`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Origin: "https://evil.example.test" },
      body: "{}",
    });
    assert.equal(untrustedBrowserStart.status, 403);
    assert.equal(untrustedBrowserStart.headers.get("access-control-allow-origin"), null);
    assert.equal((await untrustedBrowserStart.json()).error.message, "pairing origin not allowed");
    assert.equal(requests.length, 0);

    const validNonce = "a".repeat(43);
    const startedResponse = await fetch(`${baseUrl}/pairing/start`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Origin: "https://chat.puffo.ai" },
      body: JSON.stringify({ pairingPublicNonce: validNonce }),
    });
    assert.equal(startedResponse.status, 200);
    const started = await startedResponse.json();
    assert.equal(started.status, "pending");
    assert.equal(started.pairingId, "pair_1");
    assert.equal(started.userCode, "ABCD-EFGH");
    assert.equal((requests[0] as any).pairingPublicNonce, validNonce);
    assert.equal((requests[0] as any).daemonVersion, "0.1.0");
    assert.match((requests[0] as any).localApiOrigin, /^http:\/\/127\.0\.0\.1:/);

    const malformedPairingId = await getStatus(`${baseUrl}/pairing/%E0%A4%A`);
    assert.equal(malformedPairingId.status, 400);
    const malformedPairingIdBody = await malformedPairingId.json();
    assert.equal(malformedPairingIdBody.error.code, "bad_request");
    assert.match(malformedPairingIdBody.error.message, /pairing id/);
    assert.equal(requests.length, 1);

    const unsafePairingId = await getStatus(`${baseUrl}/pairing/..%2Fpair_1`);
    assert.equal(unsafePairingId.status, 400);
    const unsafePairingIdBody = await unsafePairingId.json();
    assert.equal(unsafePairingIdBody.error.code, "bad_request");
    assert.match(unsafePairingIdBody.error.message, /safe identifier/);
    assert.equal(requests.length, 1);

    const untrustedBrowserPoll = await fetch(`${baseUrl}/pairing/pair_1`, {
      headers: { Origin: "https://evil.example.test" },
    });
    assert.equal(untrustedBrowserPoll.status, 403);
    assert.equal(untrustedBrowserPoll.headers.get("access-control-allow-origin"), null);
    assert.equal((await untrustedBrowserPoll.json()).error.message, "pairing origin not allowed");
    assert.equal(pollCount, 0);

    const confirmedResponse = await fetch(`${baseUrl}/pairing/pair_1`, {
      headers: { Origin: "https://chat.puffo.ai" },
    });
    assert.equal(confirmedResponse.status, 200);
    const confirmed = await confirmedResponse.json();
    assert.equal(confirmed.status, "confirmed");
    assert.equal(confirmed.operatorSlug, "alice");
    assert.equal(confirmed.accountId, "acct_alice");
    assert.equal(core.confirmInput?.authToken, "agtok_server_secret");
    assert.equal(core.confirmInput?.pairingId, "pair_1");
    assert.equal(confirmed.core.status, "pairing_required");
    assert.equal(confirmed.localGrant.accountId, "acct_alice");
    assert.equal(confirmed.localGrant.operatorSlug, "alice");
    assert.equal(confirmed.localGrant.scopes[0], "management");
    assert.doesNotMatch(JSON.stringify(confirmed), /agtok_server_secret/);

    const health = await getJson(`${baseUrl}/health`);
    assert.equal(health.binding.accountId, "acct_alice");
    assert.equal(health.binding.operatorSlug, "alice");
    assert.equal(health.binding.deviceId, "native-sidecar-prod");
    const legacyInfo = await getJson(`${baseUrl}/v1/info`);
    assert.equal(legacyInfo.paired, true);
    assert.equal(legacyInfo.paired_slug, "alice");
    assert.equal(legacyInfo.paired_device_id, "native-sidecar-prod");
    const unknownConfig = await getJson(`${baseUrl}/configuration`);
    assert.equal(unknownConfig.state, "configured_for_existing_account");
    assert.equal(unknownConfig.configured, false);
    const aliceConfig = await getJson(`${baseUrl}/configuration?accountId=acct_alice&operatorSlug=alice`);
    assert.equal(aliceConfig.state, "configured_for_current_account");
    assert.equal(aliceConfig.configured, true);
    const bobConfig = await getJson(`${baseUrl}/configuration?accountId=acct_bob&operatorSlug=bob`);
    assert.equal(bobConfig.state, "configured_for_different_account");
    assert.equal(bobConfig.configured, false);
    const typoConfig = await getStatus(`${baseUrl}/configuration?accountID=acct_alice`);
    assert.equal(typoConfig.status, 400);
    assert.equal((await typoConfig.json()).error.code, "bad_request");
    const duplicateConfig = await getStatus(`${baseUrl}/configuration?accountId=acct_alice&accountId=acct_bob`);
    assert.equal(duplicateConfig.status, 400);
    assert.equal((await duplicateConfig.json()).error.code, "bad_request");
    const invalidConfigSlug = await getStatus(`${baseUrl}/configuration?operatorSlug=Alice`);
    assert.equal(invalidConfigSlug.status, 400);
    assert.equal((await invalidConfigSlug.json()).error.code, "bad_request");

    const aliceAgent = await postJsonStatus(`${baseUrl}/agents`, 201, {
      name: "Alice Agent",
      provider: "codex",
      accessMode: "safe",
      coreIdentity: {
        operatorSlug: "alice",
        agentSlug: "alice-agent",
        identityType: "agent",
        declaredOperatorPublicKey: "alice-public-key",
      },
    }, "local-token");
    assert.equal(aliceAgent.coreIdentity.operatorSlug, "alice");

    const grantWithoutAccount = await fetch(`${baseUrl}/agents`, {
      headers: authHeaders(confirmed.localGrant.token),
    });
    assert.equal(grantWithoutAccount.status, 401);
    const grantForBob = await fetch(`${baseUrl}/agents`, {
      headers: { ...authHeaders(confirmed.localGrant.token), ...accountHeaders("acct_bob", "bob") },
    });
    assert.equal(grantForBob.status, 401);
    const legacyGrantAfterPairing = await fetch(`${baseUrl}/agents`, {
      headers: { ...authHeaders(legacyGrant.token), ...accountHeaders("acct_legacy", "legacy") },
    });
    assert.equal(legacyGrantAfterPairing.status, 401);
    const controlTokenForBob = await fetch(`${baseUrl}/agents`, {
      headers: { ...authHeaders("local-token"), ...accountHeaders("acct_bob", "bob") },
    });
    assert.equal(controlTokenForBob.status, 409);
    assert.equal((await controlTokenForBob.json()).error.code, "account_mismatch");
    const invalidOperatorHeader = await fetch(`${baseUrl}/agents`, {
      headers: { ...authHeaders("local-token"), ...accountHeaders("acct_alice", "Alice") },
    });
    assert.equal(invalidOperatorHeader.status, 400);
    assert.equal((await invalidOperatorHeader.json()).error.code, "bad_request");
    const oversizedAccountHeader = await fetch(`${baseUrl}/agents`, {
      headers: { ...authHeaders("local-token"), ...accountHeaders("a".repeat(257), "alice") },
    });
    assert.equal(oversizedAccountHeader.status, 400);
    assert.equal((await oversizedAccountHeader.json()).error.code, "bad_request");
    const grantForAlice = await fetch(`${baseUrl}/agents`, {
      headers: { ...authHeaders(confirmed.localGrant.token), ...accountHeaders("acct_alice", "alice") },
    });
    assert.equal(grantForAlice.status, 200);
    assert.equal((await grantForAlice.json()).agents.length, 1);

    core.confirmInput = undefined;
    const secondPoll = await getJson(`${baseUrl}/pairing/pair_1`);
    assert.equal(secondPoll.status, "confirmed");
    assert.equal(core.confirmInput, undefined);
    assert.equal(secondPoll.localWebGrant, undefined);
    assert.equal(secondPoll.localGrant, undefined);
    const healthAfterSecondPoll = await getJson(`${baseUrl}/health`);
    assert.equal(healthAfterSecondPoll.binding.deviceId, "native-sidecar-prod");
  } finally {
    await api.close();
    await closeTestServer(backend);
  }
});

test("ApiServer ignores unsafe server-requested local Web grant TTLs", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-api-server-pairing-ttl-"));
  const store = new StateStore(root);
  await store.init();
  const core = new RecordingPairingCoreNative();
  const logs = new LogStore(root);
  const backend = http.createServer((req, res) => {
    if (req.method === "GET" && req.url === "/agent-core/pairings/pair_bad_ttl") {
      return sendTestJson(res, 200, {
        status: "confirmed",
        expiresAt: "2026-05-06T19:00:00.000Z",
        operatorSlug: "alice",
        accountId: "acct_alice",
        authToken: "agtok_server_secret",
        localWebGrant: { mode: "daemon_mints", ttlMs: 30 * 24 * 60 * 60 * 1000 },
      });
    }
    sendTestJson(res, 404, { error: "not_found" });
  });
  await listenTestServer(backend);
  const api = new ApiServer({
    store,
    logs,
    runtime: new RuntimeManager(store, logs),
    detector: testDetector(),
    core,
    pairing: new ServerPairingGateway(core, store, { serverUrl: urlFor(backend) }),
    apiToken: "local-token",
  });
  const server = await api.listen(0, "127.0.0.1");

  try {
    const confirmed = await getJson(`${urlFor(server)}/pairing/pair_bad_ttl`, "local-token");
    assert.equal(confirmed.status, "confirmed");
    assert.equal(core.confirmInput?.authToken, "agtok_server_secret");
    assert.equal(confirmed.localWebGrant, undefined);
    assert.equal(confirmed.localGrant, undefined);
    const grants = await store.listLocalAccessGrants();
    assert.equal(grants.length, 0);
  } finally {
    await api.close();
    await closeTestServer(backend);
  }
});

test("ApiServer rejects malformed server pairing responses", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-api-server-pairing-malformed-"));
  const store = new StateStore(root);
  await store.init();
  const logs = new LogStore(root);
  const core = new RecordingPairingCoreNative();
  const backend = http.createServer((req, res) => {
    if (req.method === "GET" && req.url === "/agent-core/pairings/pair_bad_response") {
      return sendTestJson(res, 200, {
        status: "surprise",
        expiresAt: "2026-05-06T19:00:00.000Z",
      });
    }
    if (req.method === "GET" && req.url === "/agent-core/pairings/pair_bad_auth_token") {
      return sendTestJson(res, 200, {
        status: "confirmed",
        expiresAt: "2026-05-06T19:00:00.000Z",
        operatorSlug: "alice",
        accountId: "acct_alice",
        authToken: "a".repeat(16 * 1024 + 1),
      });
    }
    if (req.method === "GET" && req.url === "/agent-core/pairings/pair_huge_response") {
      return sendTestJson(res, 200, {
        status: "pending",
        expiresAt: "2026-05-06T19:00:00.000Z",
        padding: "x".repeat(70 * 1024),
      });
    }
    if (req.method === "GET" && req.url === "/agent-core/pairings/pair_bad_content_type") {
      res.writeHead(200, { "Content-Type": "text/html" });
      return res.end(JSON.stringify({
        status: "pending",
        expiresAt: "2026-05-06T19:00:00.000Z",
      }));
    }
    if (req.method === "GET" && req.url === "/agent-core/pairings/pair_pending_auth_fields") {
      return sendTestJson(res, 200, {
        status: "pending",
        expiresAt: "2026-05-06T19:00:00.000Z",
        authToken: "agtok_must_not_be_accepted",
        operatorBootstrap: { kind: "existing_local_identity", operatorSlug: "alice" },
        localWebGrant: { mode: "daemon_mints", ttlMs: 60_000 },
      });
    }
    sendTestJson(res, 404, { error: "not_found" });
  });
  await listenTestServer(backend);
  const api = new ApiServer({
    store,
    logs,
    runtime: new RuntimeManager(store, logs),
    detector: testDetector(),
    core,
    pairing: new ServerPairingGateway(core, store, { serverUrl: urlFor(backend) }),
    apiToken: "local-token",
  });
  const server = await api.listen(0, "127.0.0.1");

  try {
    const response = await getStatus(`${urlFor(server)}/pairing/pair_bad_response`, "local-token");
    assert.equal(response.status, 500);
    const body = await response.json() as any;
    assert.equal(body.error.code, "internal_error");
    assert.match(body.error.message, /server pairing response was invalid/);

    const badAuthToken = await getStatus(`${urlFor(server)}/pairing/pair_bad_auth_token`, "local-token");
    assert.equal(badAuthToken.status, 500);
    const badAuthTokenBody = await badAuthToken.json() as any;
    assert.equal(badAuthTokenBody.error.code, "internal_error");
    assert.match(badAuthTokenBody.error.message, /server pairing response was invalid/);
    assert.equal(core.confirmInput, undefined);

    const hugeResponse = await getStatus(`${urlFor(server)}/pairing/pair_huge_response`, "local-token");
    assert.equal(hugeResponse.status, 500);
    const hugeResponseBody = await hugeResponse.json() as any;
    assert.equal(hugeResponseBody.error.code, "internal_error");
    assert.match(hugeResponseBody.error.message, /server pairing response was too large/);

    const badContentType = await getStatus(`${urlFor(server)}/pairing/pair_bad_content_type`, "local-token");
    assert.equal(badContentType.status, 500);
    const badContentTypeBody = await badContentType.json() as any;
    assert.equal(badContentTypeBody.error.code, "internal_error");
    assert.match(badContentTypeBody.error.message, /unsupported content type/);

    const pendingAuthFields = await getStatus(`${urlFor(server)}/pairing/pair_pending_auth_fields`, "local-token");
    assert.equal(pendingAuthFields.status, 500);
    const pendingAuthFieldsBody = await pendingAuthFields.json() as any;
    assert.equal(pendingAuthFieldsBody.error.code, "internal_error");
    assert.match(pendingAuthFieldsBody.error.message, /server pairing response was invalid/);
    assert.equal(core.confirmInput, undefined);
  } finally {
    await api.close();
    await closeTestServer(backend);
  }
});

test("ApiServer rejects malformed server pairing start responses", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-api-server-pairing-start-malformed-"));
  const store = new StateStore(root);
  await store.init();
  const logs = new LogStore(root);
  let startResponse: Record<string, unknown> = {
    pairingId: "bad/pairing",
    userCode: "ABCD-EFGH",
    confirmUrl: "javascript:alert(1)",
    expiresAt: "not-a-date",
    pollAfterMs: 1,
  };
  const backend = http.createServer((req, res) => {
    if (req.method === "POST" && req.url === "/agent-core/pairings") {
      return sendTestJson(res, 201, startResponse);
    }
    sendTestJson(res, 404, { error: "not_found" });
  });
  await listenTestServer(backend);
  const api = new ApiServer({
    store,
    logs,
    runtime: new RuntimeManager(store, logs),
    detector: testDetector(),
    core: new RecordingPairingCoreNative(),
    pairing: new ServerPairingGateway(new RecordingPairingCoreNative(), store, { serverUrl: urlFor(backend) }),
    apiToken: "local-token",
  });
  const server = await api.listen(0, "127.0.0.1");

  try {
    const response = await postJsonStatus(`${urlFor(server)}/pairing/start`, 500, {}, "local-token");
    assert.equal(response.error.code, "internal_error");
    startResponse = {
      pairingId: "pair_1",
      userCode: "ABCD-EFGH",
      confirmUrl: "https://alice:secret@app.example.test/agents/pair?pairingId=pair_1",
      expiresAt: "2026-05-06T19:00:00.000Z",
      pollAfterMs: 1000,
    };
    const credentialUrl = await postJsonStatus(`${urlFor(server)}/pairing/start`, 500, {}, "local-token");
    assert.equal(credentialUrl.error.code, "internal_error");
    assert.match(credentialUrl.error.message, /server pairing response was invalid/);
    startResponse = {
      pairingId: "pair_1",
      userCode: "ABCD-EFGH",
      confirmUrl: "http://app.example.test/agents/pair?pairingId=pair_1",
      expiresAt: "2026-05-06T19:00:00.000Z",
      pollAfterMs: 1000,
    };
    const insecureRemoteUrl = await postJsonStatus(`${urlFor(server)}/pairing/start`, 500, {}, "local-token");
    assert.equal(insecureRemoteUrl.error.code, "internal_error");
    assert.match(insecureRemoteUrl.error.message, /server pairing response was invalid/);
    startResponse = {
      pairingId: "pair_1",
      userCode: "ABCD-EFGH",
      confirmUrl: "https://app.example.test/agents/pair?pairingId=pair_1",
      expiresAt: "2026-05-06T19:00:00.000Z",
      pollAfterMs: 1000,
      root_secret_key: "must-not-reach-web",
    };
    const extraField = await postJsonStatus(`${urlFor(server)}/pairing/start`, 500, {}, "local-token");
    assert.equal(extraField.error.code, "internal_error");
    assert.match(extraField.error.message, /server pairing response was invalid/);
    startResponse = {
      pairingId: "pair_1",
      userCode: "ABCD-EFGH",
      confirmUrl: "http://127.0.0.1:3000/agents/pair?pairingId=pair_1",
      expiresAt: "2026-05-06T19:00:00.000Z",
      pollAfterMs: 1000,
    };
    const loopbackHttpUrl = await postJsonStatus(`${urlFor(server)}/pairing/start`, 200, {}, "local-token");
    assert.equal(loopbackHttpUrl.confirmUrl, "http://127.0.0.1:3000/agents/pair?pairingId=pair_1");
  } finally {
    await api.close();
    await closeTestServer(backend);
  }
});

test("ApiServer does not follow server pairing redirects", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-api-server-pairing-redirect-"));
  const store = new StateStore(root);
  await store.init();
  const logs = new LogStore(root);
  let followed = false;
  const backend = http.createServer((req, res) => {
    if (req.method === "POST" && req.url === "/agent-core/pairings") {
      res.writeHead(307, { Location: "/agent-core/pairings/redirected" });
      return res.end();
    }
    if (req.method === "POST" && req.url === "/agent-core/pairings/redirected") {
      followed = true;
      return sendTestJson(res, 201, {
        pairingId: "pair_1",
        userCode: "ABCD-EFGH",
        confirmUrl: "https://app.example.test/agents/pair?pairingId=pair_1",
        expiresAt: "2026-05-06T19:00:00.000Z",
        pollAfterMs: 1000,
      });
    }
    sendTestJson(res, 404, { error: "not_found" });
  });
  await listenTestServer(backend);
  const api = new ApiServer({
    store,
    logs,
    runtime: new RuntimeManager(store, logs),
    detector: testDetector(),
    core: new RecordingPairingCoreNative(),
    pairing: new ServerPairingGateway(new RecordingPairingCoreNative(), store, { serverUrl: urlFor(backend) }),
    apiToken: "local-token",
  });
  const server = await api.listen(0, "127.0.0.1");

  try {
    const response = await postJsonStatus(`${urlFor(server)}/pairing/start`, 500, {}, "local-token");
    assert.equal(response.error.code, "internal_error");
    assert.equal(followed, false);
  } finally {
    await api.close();
    await closeTestServer(backend);
  }
});

test("ApiServer rejects malformed confirmed server pairing identity fields", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-api-server-pairing-malformed-identity-"));
  const store = new StateStore(root);
  await store.init();
  const logs = new LogStore(root);
  const core = new RecordingPairingCoreNative();
  const backend = http.createServer((req, res) => {
    if (req.method === "GET" && req.url === "/agent-core/pairings/pair_bad_operator") {
      return sendTestJson(res, 200, {
        status: "confirmed",
        expiresAt: "2026-05-06T19:00:00.000Z",
        operatorSlug: "Alice",
        accountId: "acct_alice",
        authToken: "agtok_server_secret",
      });
    }
    if (req.method === "GET" && req.url === "/agent-core/pairings/pair_bad_bootstrap") {
      return sendTestJson(res, 200, {
        status: "confirmed",
        expiresAt: "2026-05-06T19:00:00.000Z",
        operatorSlug: "alice",
        accountId: "acct_alice",
        authToken: "agtok_server_secret",
        operatorBootstrap: { kind: "existing_local_identity", operatorSlug: "mallory" },
      });
    }
    if (req.method === "GET" && req.url === "/agent-core/pairings/pair_bad_account") {
      return sendTestJson(res, 200, {
        status: "confirmed",
        expiresAt: "2026-05-06T19:00:00.000Z",
        operatorSlug: "alice",
        accountId: "a".repeat(257),
        authToken: "agtok_server_secret",
      });
    }
    if (req.method === "GET" && req.url === "/agent-core/pairings/pair_bootstrap_secret") {
      return sendTestJson(res, 200, {
        status: "confirmed",
        expiresAt: "2026-05-06T19:00:00.000Z",
        operatorSlug: "alice",
        accountId: "acct_alice",
        authToken: "agtok_server_secret",
        operatorBootstrap: {
          kind: "existing_local_identity",
          operatorSlug: "alice",
          root_secret_key: "must-not-reach-web",
        },
      });
    }
    if (req.method === "GET" && req.url === "/agent-core/pairings/pair_top_level_secret") {
      return sendTestJson(res, 200, {
        status: "confirmed",
        expiresAt: "2026-05-06T19:00:00.000Z",
        operatorSlug: "alice",
        accountId: "acct_alice",
        authToken: "agtok_server_secret",
        root_secret_key: "must-not-reach-web",
      });
    }
    if (req.method === "GET" && req.url === "/agent-core/pairings/pair_restore_bootstrap") {
      return sendTestJson(res, 200, {
        status: "confirmed",
        expiresAt: "2026-05-06T19:00:00.000Z",
        operatorSlug: "alice",
        accountId: "acct_alice",
        operatorBootstrap: {
          kind: "restore_or_enroll",
          operatorSlug: "alice",
          payload: { mode: "cloud_backup", root_secret_key: "must-not-reach-web" },
        },
      });
    }
    sendTestJson(res, 404, { error: "not_found" });
  });
  await listenTestServer(backend);
  const api = new ApiServer({
    store,
    logs,
    runtime: new RuntimeManager(store, logs),
    detector: testDetector(),
    core,
    pairing: new ServerPairingGateway(core, store, { serverUrl: urlFor(backend) }),
    apiToken: "local-token",
  });
  const server = await api.listen(0, "127.0.0.1");

  try {
    const badOperator = await getStatus(`${urlFor(server)}/pairing/pair_bad_operator`, "local-token");
    assert.equal(badOperator.status, 500);
    assert.equal(core.confirmInput, undefined);

    const badBootstrap = await getStatus(`${urlFor(server)}/pairing/pair_bad_bootstrap`, "local-token");
    assert.equal(badBootstrap.status, 500);
    assert.equal(core.confirmInput, undefined);

    const badAccount = await getStatus(`${urlFor(server)}/pairing/pair_bad_account`, "local-token");
    assert.equal(badAccount.status, 500);
    assert.equal(core.confirmInput, undefined);

    const secretBootstrap = await getStatus(`${urlFor(server)}/pairing/pair_bootstrap_secret`, "local-token");
    assert.equal(secretBootstrap.status, 500);
    assert.equal(core.confirmInput, undefined);

    const topLevelSecret = await getStatus(`${urlFor(server)}/pairing/pair_top_level_secret`, "local-token");
    assert.equal(topLevelSecret.status, 500);
    assert.equal(core.confirmInput, undefined);

    const restoreBootstrap = await getJson(`${urlFor(server)}/pairing/pair_restore_bootstrap`, "local-token");
    assert.equal(restoreBootstrap.status, "confirmed");
    assert.deepEqual(restoreBootstrap.operatorBootstrap, {
      kind: "restore_or_enroll",
      operatorSlug: "alice",
    });
    assert.doesNotMatch(JSON.stringify(restoreBootstrap), /must-not-reach-web/);
  } finally {
    await api.close();
    await closeTestServer(backend);
  }
});

test("ApiServer can restrict browser CORS origins without blocking local tokened clients", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-api-cors-"));
  const store = new StateStore(root);
  await store.init();
  const logs = new LogStore(root);
  const runtime = new RuntimeManager(store, logs);
  const api = new ApiServer({
    store,
    logs,
    runtime,
    detector: testDetector(),
    core: new UnavailableCoreNative(),
    apiToken: "local-token",
    allowedOrigins: ["https://app.example.test"],
  });
  const server = await api.listen(0, "127.0.0.1");
  const baseUrl = urlFor(server);

  try {
    const allowedPreflight = await fetch(`${baseUrl}/agents`, {
      method: "OPTIONS",
      headers: {
        Origin: "https://app.example.test",
        "Access-Control-Request-Headers": "X-Agent-Core-Token",
        "Access-Control-Request-Private-Network": "true",
      },
    });
    assert.equal(allowedPreflight.status, 204);
    assert.equal(allowedPreflight.headers.get("access-control-allow-origin"), "https://app.example.test");
    assert.match(allowedPreflight.headers.get("vary") ?? "", /Origin/);
    assert.match(allowedPreflight.headers.get("vary") ?? "", /Access-Control-Request-Private-Network/);

    const blockedPreflight = await fetch(`${baseUrl}/agents`, {
      method: "OPTIONS",
      headers: {
        Origin: "https://evil.example.test",
        "Access-Control-Request-Headers": "X-Agent-Core-Token",
      },
    });
    assert.equal(blockedPreflight.status, 403);
    assert.equal(blockedPreflight.headers.get("access-control-allow-origin"), null);
    assert.equal((await blockedPreflight.json()).error.code, "forbidden");

    const blockedManagement = await fetch(`${baseUrl}/agents`, {
      headers: {
        Origin: "https://evil.example.test",
        Authorization: "Bearer local-token",
      },
    });
    assert.equal(blockedManagement.status, 403);
    assert.equal((await blockedManagement.json()).error.message, "origin not allowed");

    const listed = await getJson(`${baseUrl}/agents`, "local-token");
    assert.deepEqual(listed.agents, []);
  } finally {
    await api.close();
  }
});

test("ApiServer validates configured CORS origins", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-api-cors-config-"));
  const store = new StateStore(root);
  await store.init();
  const deps = {
    store,
    logs: new LogStore(root),
    runtime: new RuntimeManager(store, new LogStore(root)),
    detector: testDetector(),
    core: new UnavailableCoreNative(),
    apiToken: "local-token",
  };

  assert.throws(
    () => new ApiServer({ ...deps, allowedOrigins: ["https://app.example.test/path"] }),
    /origins without path/,
  );
  assert.throws(
    () => new ApiServer({ ...deps, allowedOrigins: ["file:///tmp/app"] }),
    /http and https/,
  );

  const wildcard = new ApiServer({ ...deps, allowedOrigins: ["*"] });
  const server = await wildcard.listen(0, "127.0.0.1");
  const baseUrl = urlFor(server);

  try {
    const preflight = await fetch(`${baseUrl}/agents`, {
      method: "OPTIONS",
      headers: { Origin: "https://any.example.test" },
    });
    assert.equal(preflight.status, 204);
    assert.equal(preflight.headers.get("access-control-allow-origin"), "*");
  } finally {
    await wildcard.close();
  }
});

test("ApiServer issues and revokes short-lived local grants with the control token only", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-api-grant-issue-"));
  const store = new StateStore(root);
  await store.init();
  const logs = new LogStore(root);
  const runtime = new RuntimeManager(store, logs);
  const api = new ApiServer({
    store,
    logs,
    runtime,
    detector: testDetector(),
    core: new UnavailableCoreNative(),
    apiToken: "local-token",
  });
  const server = await api.listen(0, "127.0.0.1");
  const baseUrl = urlFor(server);

  try {
    const unauthenticated = await postJsonStatus(`${baseUrl}/local-grants`, 401, {});
    assert.equal(unauthenticated.error.code, "unauthorized");

    const invalidTtl = await postJsonStatus(`${baseUrl}/local-grants`, 400, { ttlMs: 0 }, "local-token");
    assert.equal(invalidTtl.error.code, "bad_request");
    assert.match(invalidTtl.error.message, /ttlMs/);

    const tooLongTtl = await postJsonStatus(`${baseUrl}/local-grants`, 400, { ttlMs: 48 * 60 * 60 * 1000 }, "local-token");
    assert.equal(tooLongTtl.error.code, "bad_request");

    const arrayBody = await postJsonStatus(`${baseUrl}/local-grants`, 400, [], "local-token");
    assert.equal(arrayBody.error.code, "bad_request");
    assert.match(arrayBody.error.message, /JSON object/);
    const primitiveBody = await postJsonStatus(`${baseUrl}/local-grants`, 400, "not-object", "local-token");
    assert.equal(primitiveBody.error.code, "bad_request");
    assert.match(primitiveBody.error.message, /JSON object/);

    const unsupportedField = await postJsonStatus(
      `${baseUrl}/local-grants`,
      400,
      { ttlMs: 60_000, scopes: ["management"], token: "must-not-enter-state" },
      "local-token",
    );
    assert.equal(unsupportedField.error.code, "bad_request");
    assert.match(unsupportedField.error.message, /scopes|token/);

    const grant = await postJsonStatus(`${baseUrl}/local-grants`, 201, { ttlMs: 60_000 }, "local-token");
    assert.match(grant.id, /^[0-9a-f-]+$/i);
    assert.match(grant.token, /^[A-Za-z0-9_-]+$/);
    assert.deepEqual(grant.scopes, ["management"]);
    assert.equal(typeof grant.expiresAt, "string");

    const saved = await store.readDeviceState();
    assert.equal(saved?.localGrants?.length, 1);
    assert.notEqual(saved?.localGrants?.[0]?.tokenHash, grant.token);

    const grantList = await getJson(`${baseUrl}/local-grants`, "local-token");
    assert.equal(grantList.grants.length, 1);
    assert.equal(grantList.grants[0].id, grant.id);
    assert.equal(grantList.grants[0].active, true);
    assert.deepEqual(grantList.grants[0].scopes, ["management"]);
    assert.equal(grantList.grants[0].token, undefined);
    assert.equal(grantList.grants[0].tokenHash, undefined);

    const listed = await getJson(`${baseUrl}/agents`, grant.token);
    assert.deepEqual(listed.agents, []);

    const grantCannotList = await getStatus(`${baseUrl}/local-grants`, grant.token);
    assert.equal(grantCannotList.status, 401);
    assert.equal((await grantCannotList.json()).error.code, "unauthorized");

    const grantCannotMint = await postJsonStatus(`${baseUrl}/local-grants`, 401, {}, grant.token);
    assert.equal(grantCannotMint.error.code, "unauthorized");

    const grantCannotRevoke = await deleteJsonStatus(`${baseUrl}/local-grants/${grant.id}`, 401, grant.token);
    assert.equal(grantCannotRevoke.error.code, "unauthorized");

    const missing = await deleteJsonStatus(`${baseUrl}/local-grants/00000000-0000-4000-8000-000000000000`, 404, "local-token");
    assert.equal(missing.error.code, "not_found");

    const revokeWithBody = await deleteJsonStatus(
      `${baseUrl}/local-grants/${grant.id}`,
      400,
      "local-token",
      { reason: "user-requested", token: "must-not-be-accepted" },
    );
    assert.equal(revokeWithBody.error.code, "bad_request");
    assert.match(revokeWithBody.error.message, /body must be empty/);
    assert.equal(await store.verifyLocalAccessGrant(grant.token), true);

    const revoked = await deleteJsonStatus(`${baseUrl}/local-grants/${grant.id}`, 200, "local-token");
    assert.deepEqual(revoked, { id: grant.id, revoked: true });

    const revokedGrantList = await getJson(`${baseUrl}/local-grants`, "local-token");
    assert.equal(revokedGrantList.grants.length, 1);
    assert.equal(revokedGrantList.grants[0].id, grant.id);
    assert.equal(revokedGrantList.grants[0].active, false);
    assert.equal(typeof revokedGrantList.grants[0].revokedAt, "string");

    const revokedResult = await getStatus(`${baseUrl}/agents`, grant.token);
    assert.equal(revokedResult.status, 401);
  } finally {
    await api.close();
  }
});

test("ApiServer rotates the local control token with the control token only", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-api-token-rotate-"));
  const store = new StateStore(root);
  await store.init();
  const logs = new LogStore(root);
  const runtime = new RuntimeManager(store, logs);
  const api = new ApiServer({
    store,
    logs,
    runtime,
    detector: testDetector(),
    core: new UnavailableCoreNative(),
    apiToken: "local-token",
  });
  const server = await api.listen(0, "127.0.0.1");
  const baseUrl = urlFor(server);

  try {
    const grant = await postJsonStatus(`${baseUrl}/local-grants`, 201, { ttlMs: 60_000 }, "local-token");
    const grantCannotRotate = await postJsonStatus(`${baseUrl}/local-control-token/rotate`, 401, {}, grant.token);
    assert.equal(grantCannotRotate.error.code, "unauthorized");

    const rotateWithBody = await postJsonStatus(
      `${baseUrl}/local-control-token/rotate`,
      400,
      { reason: "user-requested", token: "must-not-be-accepted" },
      "local-token",
    );
    assert.equal(rotateWithBody.error.code, "bad_request");
    assert.match(rotateWithBody.error.message, /body must be empty/);
    const rotateWithNullBody = await postJsonStatus(
      `${baseUrl}/local-control-token/rotate`,
      400,
      null,
      "local-token",
    );
    assert.equal(rotateWithNullBody.error.code, "bad_request");
    assert.match(rotateWithNullBody.error.message, /JSON object/);

    const rotated = await postJsonStatus(`${baseUrl}/local-control-token/rotate`, 200, {}, "local-token");
    assert.equal(rotated.rotated, true);
    assert.equal(rotated.grantsRevoked, true);
    assert.match(rotated.token, /^[A-Za-z0-9_-]+$/);
    assert.notEqual(rotated.token, "local-token");

    const oldTokenResult = await getStatus(`${baseUrl}/agents`, "local-token");
    assert.equal(oldTokenResult.status, 401);
    const oldGrantResult = await getStatus(`${baseUrl}/agents`, grant.token);
    assert.equal(oldGrantResult.status, 401);

    const listed = await getJson(`${baseUrl}/agents`, rotated.token);
    assert.deepEqual(listed.agents, []);
    const secondGrant = await postJsonStatus(`${baseUrl}/local-grants`, 201, { ttlMs: 60_000 }, rotated.token);
    assert.match(secondGrant.token, /^[A-Za-z0-9_-]+$/);
  } finally {
    await api.close();
  }
});

test("ApiServer rejects non-loopback Host before preflight handling", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-api-host-preflight-"));
  const store = new StateStore(root);
  await store.init();
  const logs = new LogStore(root);
  const runtime = new RuntimeManager(store, logs);
  const api = new ApiServer({
    store,
    logs,
    runtime,
    detector: testDetector(),
    core: new UnavailableCoreNative(),
    apiToken: "local-token",
  });
  const server = await api.listen(0, "127.0.0.1");

  try {
    const status = await requestWithHost(server, "OPTIONS", "/agents", "127.0.0.1.evil.test");
    assert.equal(status, 403);
  } finally {
    await api.close();
  }
});

test("ApiServer fails closed when no local authorization token is configured", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-api-auth-closed-"));
  const store = new StateStore(root);
  await store.init();
  const logs = new LogStore(root);
  const runtime = new RuntimeManager(store, logs);
  const api = new ApiServer({
    store,
    logs,
    runtime,
    detector: testDetector(),
    core: new UnavailableCoreNative(),
  });
  const server = await api.listen(0, "127.0.0.1");
  const baseUrl = urlFor(server);

  try {
    const health = await getJson(`${baseUrl}/health`);
    assert.equal(health.authRequired, true);
    assert.equal(health.stateHome, undefined);

    const unauthorized = await getStatus(`${baseUrl}/agents`);
    assert.equal(unauthorized.status, 401);
    assert.equal((await unauthorized.json()).error.code, "unauthorized");

    const publicPairingStart = await postJsonStatus(`${baseUrl}/pairing/start`, 200, {});
    assert.equal(publicPairingStart.status, "unavailable");

    const publicPairingPoll = await getStatus(`${baseUrl}/pairing/pairing-1`);
    assert.equal(publicPairingPoll.status, 404);
    assert.equal((await publicPairingPoll.json()).error.code, "not_found");
  } finally {
    await api.close();
  }
});

test("ApiServer accepts unexpired local access grants for management routes", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-api-grant-"));
  const store = new StateStore(root);
  await store.init();
  const grant = await store.createLocalAccessGrant({ ttlMs: 60_000 });
  const expired = await store.createLocalAccessGrant({ ttlMs: 0 });
  const logs = new LogStore(root);
  const runtime = new RuntimeManager(store, logs);
  const api = new ApiServer({
    store,
    logs,
    runtime,
    detector: testDetector(),
    core: new UnavailableCoreNative(),
    apiToken: "local-token",
  });
  const server = await api.listen(0, "127.0.0.1");
  const baseUrl = urlFor(server);

  try {
    const health = await getJson(`${baseUrl}/health`, grant.token);
    assert.equal(health.stateHome, root);

    const listed = await getJson(`${baseUrl}/agents`, grant.token);
    assert.deepEqual(listed.agents, []);

    const expiredResult = await getStatus(`${baseUrl}/agents`, expired.token);
    assert.equal(expiredResult.status, 401);

    assert.equal(await store.revokeLocalAccessGrant(grant.id), true);
    const revokedResult = await getStatus(`${baseUrl}/agents`, grant.token);
    assert.equal(revokedResult.status, 401);
  } finally {
    await api.close();
  }
});

test("ApiServer redacts provider paths from public discovery", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-api-provider-paths-"));
  const store = new StateStore(root);
  await store.init();
  const logs = new LogStore(root);
  const runtime = new RuntimeManager(store, logs);
  const api = new ApiServer({
    store,
    logs,
    runtime,
    detector: pathDetector(),
    core: new UnavailableCoreNative(),
    apiToken: "local-token",
  });
  const server = await api.listen(0, "127.0.0.1");
  const baseUrl = urlFor(server);

  try {
    const publicProviders = await getJson(`${baseUrl}/providers`);
    assert.equal(publicProviders.providers.claude.path, undefined);
    assert.equal(publicProviders.providers.codex.path, undefined);
    assert.equal(publicProviders.sandbox.path, undefined);

    const authedProviders = await getJson(`${baseUrl}/providers`, "local-token");
    assert.equal(authedProviders.providers.claude.path, "/Users/alice/.local/bin/claude");
    assert.equal(authedProviders.providers.codex.path, "/Users/alice/.local/bin/codex");
    assert.equal(authedProviders.sandbox.path, process.platform === "darwin" ? "/usr/bin/sandbox-exec" : undefined);
  } finally {
    await api.close();
  }
});

test("ApiServer returns bounded redacted agent logs", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-api-logs-"));
  const store = new StateStore(root);
  await store.init();
  const logs = new LogStore(root);
  const runtime = new RuntimeManager(store, logs);
  const api = new ApiServer({
    store,
    logs,
    runtime,
    detector: testDetector(),
    core: new UnavailableCoreNative(),
    apiToken: "local-token",
  });
  const server = await api.listen(0, "127.0.0.1");
  const baseUrl = urlFor(server);

  try {
    const created = await postJsonStatus(
      `${baseUrl}/agents`,
      201,
      {
        name: "Log Agent",
        provider: "codex",
      },
      "local-token",
    );
    await logs.append(created.id, "line-1");
    await logs.append(created.id, "line-2 token=secret-value");
    await logs.append(created.id, "line-3");

    const tail = await getJson(`${baseUrl}/agents/${created.id}/logs?maxLines=2`, "local-token");
    assert.equal(tail.lines.length, 2);
    assert.match(tail.lines[0], /line-2 token=\[redacted\]/);
    assert.match(tail.lines[1], /line-3/);
    assert.doesNotMatch(JSON.stringify(tail), /secret-value/);

    const empty = await getJson(`${baseUrl}/agents/${created.id}/logs?maxLines=0`, "local-token");
    assert.deepEqual(empty.lines, []);

    const invalid = await getStatus(`${baseUrl}/agents/${created.id}/logs?maxLines=abc`, "local-token");
    assert.equal(invalid.status, 400);
    assert.equal((await invalid.json()).error.code, "bad_request");

    const tooLarge = await getStatus(`${baseUrl}/agents/${created.id}/logs?maxLines=1001`, "local-token");
    assert.equal(tooLarge.status, 400);
    assert.equal((await tooLarge.json()).error.code, "bad_request");

    const duplicateMaxLines = await getStatus(
      `${baseUrl}/agents/${created.id}/logs?maxLines=1&maxLines=2`,
      "local-token",
    );
    assert.equal(duplicateMaxLines.status, 400);
    assert.equal((await duplicateMaxLines.json()).error.code, "bad_request");

    const unsupportedLogsQuery = await getStatus(
      `${baseUrl}/agents/${created.id}/logs?tail=2`,
      "local-token",
    );
    assert.equal(unsupportedLogsQuery.status, 400);
    assert.equal((await unsupportedLogsQuery.json()).error.code, "bad_request");
  } finally {
    await api.close();
  }
});

test("ApiServer dev-inject route drives sidecar message handling when enabled", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-api-dev-"));
  const store = new StateStore(root);
  await store.init();
  const logs = new LogStore(root);
  const core = new SidecarCoreNative();
  const provider = new FakeProviderSession();
  const runtime = new RuntimeManager(store, logs, core, {
    autoStartMessageLoop: false,
    providerFactory: () => provider,
  });
  const api = new ApiServer({
    store,
    logs,
    runtime,
    detector: testDetector(),
    core,
    devRoutes: true,
    allowUnauthenticatedManagement: true,
  });
  const server = await api.listen(0, "127.0.0.1");
  const baseUrl = urlFor(server);

  try {
    const created = await postJson(`${baseUrl}/agents`, {
      name: "Alice Agent",
      provider: "codex",
      operatorSlug: "alice",
      start: true,
    });

    const missingBody = await postJsonStatus(`${baseUrl}/agents/${created.id}/dev-inject`, 400, {});
    assert.equal(missingBody.error.code, "bad_request");

    const nullBody = await postJsonStatus(`${baseUrl}/agents/${created.id}/dev-inject`, 400, null);
    assert.equal(nullBody.error.code, "bad_request");
    assert.match(nullBody.error.message, /JSON object/);

    const unsupportedField = await postJsonStatus(`${baseUrl}/agents/${created.id}/dev-inject`, 400, {
      body: "ignored",
      authToken: "must-not-enter-runtime",
    });
    assert.equal(unsupportedField.error.code, "bad_request");
    assert.match(unsupportedField.error.message, /authToken/);

    const invalidSender = await postJsonStatus(`${baseUrl}/agents/${created.id}/dev-inject`, 400, {
      senderSlug: "Alice Smith",
      body: "ignored",
    });
    assert.equal(invalidSender.error.code, "bad_request");
    assert.match(invalidSender.error.message, /senderSlug/);

    const result = await postJsonStatus(`${baseUrl}/agents/${created.id}/dev-inject`, 200, {
      senderSlug: "alice",
      body: `@${created.coreIdentity.agentSlug} status?`,
    });

    assert.equal(result.handled, 1);
    assert.match(result.injected.messageId, /^msg_/);
    assert.equal(provider.inputs.length, 1);
    assert.equal(provider.inputs[0]?.mustRespond, true);
  } finally {
    const agents = await runtime.listAgents();
    await Promise.all(agents.map((agent) => runtime.stopAgent(agent.id)));
    await api.close();
    await core.shutdown();
  }
});

function urlFor(server: http.Server): string {
  const address = server.address();
  assert(address && typeof address === "object");
  return `http://127.0.0.1:${address.port}`;
}

async function listenTestServer(server: http.Server): Promise<void> {
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      server.off("error", reject);
      resolve();
    });
  });
}

async function closeTestServer(server: http.Server): Promise<void> {
  await new Promise<void>((resolve, reject) => {
    server.close((error) => (error ? reject(error) : resolve()));
  });
}

async function readRequestJson(req: http.IncomingMessage): Promise<unknown> {
  const chunks: Buffer[] = [];
  for await (const chunk of req) chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
  return JSON.parse(Buffer.concat(chunks).toString("utf8"));
}

function sendTestJson(res: http.ServerResponse, status: number, body: unknown): void {
  res.writeHead(status, { "Content-Type": "application/json" });
  res.end(JSON.stringify(body));
}

async function getJson(url: string, token?: string): Promise<any> {
  const response = await fetch(url, token ? { headers: authHeaders(token) } : undefined);
  assert.equal(response.status, 200);
  return response.json();
}

async function getStatus(url: string, token?: string): Promise<Response> {
  return fetch(url, token ? { headers: authHeaders(token) } : undefined);
}

async function requestWithHost(server: http.Server, method: string, path: string, host: string): Promise<number> {
  const address = server.address();
  assert(address && typeof address === "object");
  return new Promise((resolve, reject) => {
    const req = http.request(
      {
        host: "127.0.0.1",
        port: address.port,
        method,
        path,
        headers: { Host: host },
      },
      (res) => {
        res.resume();
        res.on("end", () => resolve(res.statusCode ?? 0));
      },
    );
    req.on("error", reject);
    req.end();
  });
}

async function postJson(url: string, body: unknown): Promise<any> {
  return postJsonStatus(url, 201, body);
}

async function postJsonStatus(url: string, status: number, body: unknown, token?: string): Promise<any> {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders(token) },
    body: JSON.stringify(body),
  });
  assert.equal(response.status, status);
  return response.json();
}

async function postRawStatus(
  url: string,
  status: number,
  body: string,
  token?: string,
  contentType = "application/json",
): Promise<any> {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": contentType, ...authHeaders(token) },
    body,
  });
  assert.equal(response.status, status);
  return response.json();
}

async function deleteJsonStatus(url: string, status: number, token?: string, body?: unknown): Promise<any> {
  const response = await fetch(url, {
    method: "DELETE",
    headers: {
      ...authHeaders(token),
      ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
    },
    ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
  });
  assert.equal(response.status, status);
  return response.json();
}

function authHeaders(token?: string): Record<string, string> {
  return token ? { Authorization: `Bearer ${token}` } : {};
}

function accountHeaders(accountId: string, operatorSlug: string): Record<string, string> {
  return {
    "X-Agent-Core-Account-Id": accountId,
    "X-Agent-Core-Operator-Slug": operatorSlug,
  };
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

class SecretThrowingCoreNative implements CoreNative {
  async openOrCreateDevice(): Promise<never> {
    throw new Error("native failed token=secret-value");
  }

  async startPairing(): Promise<never> {
    throw new Error("pairing failed token=secret-value");
  }

  async confirmPairing(): Promise<never> {
    throw new Error("pairing failed token=secret-value");
  }

  async createAgentIdentity(): Promise<never> {
    throw new Error("identity failed token=secret-value");
  }

  async openAgentSession(): Promise<never> {
    throw new Error("session failed token=secret-value");
  }

  async syncOnce(): Promise<never> {
    throw new Error("sync failed token=secret-value");
  }

  async processPendingMessages(): Promise<never> {
    throw new Error("pending failed token=secret-value");
  }

  async sendChannelReply(): Promise<never> {
    throw new Error("reply failed token=secret-value");
  }

  async sendDirectReply(): Promise<never> {
    throw new Error("reply failed token=secret-value");
  }

  async snapshot(): Promise<never> {
    throw new Error("snapshot failed token=secret-value");
  }

  async closeSession(): Promise<void> {}
}

class RecordingPairingCoreNative implements CoreNative {
  startInput: Record<string, unknown> | undefined;
  confirmInput: Record<string, unknown> | undefined;

  async openOrCreateDevice(): Promise<DeviceStatus> {
    return {
      connected: false,
      status: "unavailable" as const,
      reason: "not paired",
    };
  }

  async startPairing(input: Record<string, unknown>) {
    this.startInput = input;
    return {
      status: "unavailable" as const,
      reason: "not wired",
    };
  }

  async confirmPairing(input: Record<string, unknown>) {
    this.confirmInput = input;
    return {
      connected: false,
      status: "pairing_required" as const,
      deviceId: "native-sidecar-prod",
      serverUrl: "https://api.puffo.ai",
      authTokenSource: "memory" as const,
    };
  }

  async openAgentSession(): Promise<never> {
    throw new Error("not implemented");
  }

  async syncOnce(): Promise<unknown> {
    return {};
  }

  async processPendingMessages() {
    return [];
  }

  async sendChannelReply() {
    return { messageId: "msg" };
  }

  async sendDirectReply() {
    return { messageId: "msg" };
  }

  async snapshot(): Promise<unknown> {
    return {};
  }

  async closeSession(): Promise<void> {}
}

class PairingRequiredIdentityCoreNative extends RecordingPairingCoreNative {
  override async openOrCreateDevice(): Promise<DeviceStatus> {
    return {
      connected: false,
      status: "pairing_required" as const,
      deviceId: "native-sidecar-prod",
      reason: "backend pairing is not complete",
    };
  }

  async createAgentIdentity(): Promise<never> {
    throw new Error("production identity registration is not implemented");
  }
}
