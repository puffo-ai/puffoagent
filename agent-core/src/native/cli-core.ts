import { execFile } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { normalizeDeclaredOperatorPublicKey } from "../platform/core-identity.js";
import { isValidCoreSlug } from "../platform/core-slug.js";
import {
  CoreNative,
  CoreSessionHandle,
  DeviceStatus,
  MessageRef,
  NativeCoreUnavailableError,
  OpenedAgentMessage,
  PairingStatus,
} from "./core.js";

export interface CliCoreNativeOptions {
  manifestPath?: string;
  features?: string[];
}

export interface AgentIdentityStatus {
  ok: boolean;
  operatorSlug: string;
  agentSlug: string;
  identityType: "agent";
  declaredOperatorPublicKey: string;
}

export class CliCoreNative implements CoreNative {
  private readonly manifestPath: string;
  private readonly features: string[];

  constructor(options: CliCoreNativeOptions = {}) {
    this.manifestPath = options.manifestPath ?? defaultManifestPath();
    this.features = options.features ?? ["dev-tools"];
  }

  async openOrCreateDevice(): Promise<DeviceStatus> {
    const health = normalizeCliHealthResponse(await this.runJson(["health"]));
    return {
      connected: health.ok,
      status: health.ok ? "ready" : "unavailable",
      deviceId: `native-${health.mode}`,
    };
  }

  async startPairing(): Promise<PairingStatus> {
    return {
      status: "unavailable",
      reason: "server pairing start is owned by the daemon pairing gateway, not the debug CLI native bridge",
      blockedBy: ["daemon_pairing_gateway"],
      nextAction: "Start the daemon normally so /pairing/start can call the backend pairing contract.",
    };
  }

  async confirmPairing(): Promise<DeviceStatus> {
    return {
      connected: false,
      status: "unavailable",
      reason: "pairing confirmation requires the persistent native sidecar",
      blockedBy: ["native_sidecar"],
      nextAction: "Use the sidecar native bridge so server-issued auth tokens can be activated without going through the debug CLI bridge.",
    };
  }

  async createAgentIdentity(input: {
    operatorSlug: string;
    agentSlug: string;
  }): Promise<AgentIdentityStatus> {
    const identity = normalizeCreateAgentIdentityInput(input);
    const result = await this.runJson([
      "dev-create-agent-identity",
      "--operator",
      identity.operatorSlug,
      "--agent",
      identity.agentSlug,
    ]);
    return normalizeAgentIdentityStatus(result);
  }

  async openAgentSession(input: Record<string, unknown>): Promise<CoreSessionHandle> {
    const slug = sessionSlugFromInput(input);
    const result = await this.runJson([
      "dev-open-agent-session",
      "--slug",
      slug,
    ]);
    return requireString(requireResponseRecord(result, "dev-open-agent-session response"), "handle");
  }

  async syncOnce(handle: CoreSessionHandle): Promise<unknown> {
    return this.runJson(["dev-sync-once", "--handle", requireSessionHandle(handle)]);
  }

  async processPendingMessages(handle: CoreSessionHandle): Promise<OpenedAgentMessage[]> {
    const result = await this.runJson([
      "dev-process-pending",
      "--handle",
      requireSessionHandle(handle),
    ]);
    const response = requireResponseRecord(result, "dev-process-pending response");
    if (!Array.isArray(response.messages)) {
      throw new Error("dev-process-pending response.messages is required");
    }
    return response.messages as OpenedAgentMessage[];
  }

  async sendChannelReply(
    handle: CoreSessionHandle,
    input: Record<string, unknown>,
  ): Promise<MessageRef> {
    validateRecordInput(input, "sendChannelReply");
    const result = await this.runJson([
      "dev-send-channel-text",
      "--handle",
      requireSessionHandle(handle),
      "--space",
      requireString(input, "spaceId"),
      "--channel",
      requireString(input, "channelId"),
      "--body",
      requireString(input, "body"),
      ...optionalFlag("--thread-root", input.threadRootId),
      ...optionalFlag("--reply-to", input.replyToId),
    ]);
    return { messageId: requireString(requireResponseRecord(result, "dev-send-channel-text response"), "message_id") };
  }

  async sendDirectReply(
    handle: CoreSessionHandle,
    input: Record<string, unknown>,
  ): Promise<MessageRef> {
    validateRecordInput(input, "sendDirectReply");
    const result = await this.runJson([
      "dev-send-direct-text",
      "--handle",
      requireSessionHandle(handle),
      "--recipient",
      requireCoreSlug(input.recipientSlug, "recipientSlug"),
      "--body",
      requireString(input, "body"),
      ...optionalFlag("--reply-to", input.replyToId),
    ]);
    return { messageId: requireString(requireResponseRecord(result, "dev-send-direct-text response"), "message_id") };
  }

  async snapshot(handle: CoreSessionHandle): Promise<unknown> {
    return this.runJson(["dev-snapshot", "--handle", requireSessionHandle(handle)]);
  }

  async closeSession(handle: CoreSessionHandle): Promise<void> {
    requireSessionHandle(handle);
    // The CLI bridge is process-per-command, so there is no persistent handle.
  }

  private async runJson<T>(nativeArgs: string[]): Promise<T> {
    const args = [
      "run",
      "--quiet",
      "--manifest-path",
      this.manifestPath,
      ...(this.features.length ? ["--features", this.features.join(",")] : []),
      "--bin",
      "agent-native-cli",
      "--",
      ...nativeArgs,
    ];
    const result = await execFileJson("cargo", args);
    if (!result.ok) {
      throw new Error(result.stderr || result.stdout || "native bridge command failed");
    }
    try {
      return JSON.parse(result.stdout) as T;
    } catch {
      throw new Error("native bridge command returned invalid JSON");
    }
  }
}

function sessionSlugFromInput(input: Record<string, unknown>): string {
  validateRecordInput(input, "openAgentSession");
  const value = input.agentSlug ?? input.slug;
  if (!isValidCoreSlug(value)) {
    throw new Error("agentSlug must be a lowercase slug to open an agent session");
  }
  return value;
}

function normalizeCreateAgentIdentityInput(input: {
  operatorSlug: string;
  agentSlug: string;
}): {
  operatorSlug: string;
  agentSlug: string;
} {
  validateRecordInput(input, "createAgentIdentity");
  if (!isValidCoreSlug(input.operatorSlug)) {
    throw new Error("operatorSlug must be a lowercase slug for native agent identity creation");
  }
  if (!isValidCoreSlug(input.agentSlug)) {
    throw new Error("agentSlug must be a lowercase slug for native agent identity creation");
  }
  return {
    operatorSlug: input.operatorSlug,
    agentSlug: input.agentSlug,
  };
}

function validateRecordInput(input: unknown, label: string): asserts input is Record<string, unknown> {
  if (!input || typeof input !== "object" || Array.isArray(input)) {
    throw new Error(`${label} input must be an object`);
  }
}

function requireResponseRecord(value: unknown, label: string): Record<string, unknown> {
  validateRecordInput(value, label);
  return value;
}

function normalizeAgentIdentityStatus(value: unknown): AgentIdentityStatus {
  const response = requireResponseRecord(value, "dev-create-agent-identity response");
  if (response.ok !== true) {
    throw new Error("dev-create-agent-identity response.ok must be true");
  }
  const operatorSlug = requireCoreSlug(response.operator_slug, "operator_slug");
  const agentSlug = requireCoreSlug(response.agent_slug, "agent_slug");
  if (response.identity_type !== "agent") {
    throw new Error("dev-create-agent-identity response.identity_type must be agent");
  }
  return {
    ok: true,
    operatorSlug,
    agentSlug,
    identityType: "agent",
    declaredOperatorPublicKey: requireDeclaredOperatorPublicKey(response.declared_operator_public_key, "declared_operator_public_key"),
  };
}

function requireDeclaredOperatorPublicKey(value: unknown, field: string): string {
  const normalized = normalizeDeclaredOperatorPublicKey(value);
  if (!normalized) {
    throw new Error(`${field} is required and must be 4096 characters or less`);
  }
  return normalized;
}

function normalizeCliHealthResponse(value: unknown): { ok: boolean; mode: string } {
  const response = requireResponseRecord(value, "native health response");
  if (typeof response.ok !== "boolean") {
    throw new Error("native health response.ok must be boolean");
  }
  return {
    ok: response.ok,
    mode: requireString(response, "mode"),
  };
}

function requireString(input: Record<string, unknown>, key: string): string {
  const value = input[key];
  if (typeof value !== "string" || !value.trim()) {
    throw new Error(`${key} is required`);
  }
  return value;
}

function requireCoreSlug(value: unknown, key: string): string {
  if (!isValidCoreSlug(value)) {
    throw new Error(`${key} must be a lowercase slug`);
  }
  return value;
}

function requireSessionHandle(handle: CoreSessionHandle): CoreSessionHandle {
  if (typeof handle !== "string" || !handle.trim()) {
    throw new Error("native session handle is required");
  }
  return handle;
}

function optionalFlag(name: string, value: unknown): string[] {
  return typeof value === "string" && value.trim() ? [name, value] : [];
}

export function defaultManifestPath(): string {
  const here = path.dirname(fileURLToPath(import.meta.url));
  const packageRoot = path.resolve(here, "../../..");
  return path.join(packageRoot, "crates", "agent-native", "Cargo.toml");
}

function execFileJson(
  command: string,
  args: string[],
): Promise<{ ok: boolean; stdout: string; stderr: string }> {
  return new Promise((resolve) => {
    execFile(
      command,
      args,
      {
        windowsHide: true,
        timeout: 120_000,
        maxBuffer: 1024 * 1024,
      },
      (error, stdout, stderr) => {
        resolve({
          ok: !error,
          stdout: String(stdout ?? "").trim(),
          stderr: String(stderr ?? "").trim(),
        });
      },
    );
  });
}
