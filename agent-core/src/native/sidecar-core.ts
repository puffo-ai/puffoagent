import { spawn, ChildProcessWithoutNullStreams } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import readline from "node:readline";
import { DEFAULT_SERVER_URL } from "../config/defaults.js";
import { normalizeDeclaredOperatorPublicKey } from "../platform/core-identity.js";
import { isValidCoreSlug } from "../platform/core-slug.js";
import { defaultStateHome } from "../platform/paths.js";
import { normalizePairingConfirmInput } from "../platform/pairing-confirm.js";
import { isValidPairingPublicNonce } from "../platform/pairing-nonce.js";
import { defaultManifestPath } from "./cli-core.js";
import {
  CoreNative,
  CoreSessionHandle,
  DevInjectedChannelMessage,
  DeviceStatus,
  MessageRef,
  OpenedAgentMessage,
  PairingStatus,
} from "./core.js";

export interface SidecarCoreNativeOptions {
  manifestPath?: string;
  features?: string[];
  binaryPath?: string;
  requestTimeoutMs?: number;
  preferPackagedBinary?: boolean;
  stateHome?: string;
}

export interface SidecarLaunchOptions {
  manifestPath: string;
  features: string[];
  binaryPath?: string;
}

export interface SidecarLaunchCommand {
  command: string;
  args: string[];
  diagnostic?: string;
}

interface RpcResponse<T> {
  id: number;
  ok: boolean;
  result?: T;
  error?: string;
}

interface PendingRequest<T> {
  resolve(value: T): void;
  reject(error: Error): void;
}

export class SidecarCoreNative implements CoreNative {
  private readonly manifestPath: string;
  private readonly features: string[];
  private readonly binaryPath: string | undefined;
  private readonly requestTimeoutMs: number;
  private readonly stateHome: string | undefined;
  private child: ChildProcessWithoutNullStreams | undefined;
  private nextId = 1;
  private readonly pending = new Map<number, PendingRequest<unknown>>();
  private stderrTail = "";

  constructor(options: SidecarCoreNativeOptions = {}) {
    this.manifestPath = options.manifestPath ?? defaultManifestPath();
    this.features = options.features ?? ["dev-tools"];
    this.binaryPath =
      options.binaryPath ??
      process.env.AGENT_CORE_SIDECAR_BIN ??
      (options.preferPackagedBinary === false ? undefined : defaultSidecarBinaryPath());
    this.requestTimeoutMs = options.requestTimeoutMs ?? 120_000;
    this.stateHome = options.stateHome;
  }

  async openOrCreateDevice(): Promise<DeviceStatus> {
    const result = await this.request("health");
    return normalizeDeviceStatus(result, "health response");
  }

  async startPairing(input: Record<string, unknown>): Promise<PairingStatus> {
    const result = await this.request("startPairing", normalizeStartPairingInput(input));
    return normalizePairingStatus(result, "startPairing response");
  }

  async confirmPairing(input: Record<string, unknown>): Promise<DeviceStatus> {
    const result = await this.request("confirmPairing", normalizePairingConfirmInput(input));
    return normalizeDeviceStatus(result, "confirmPairing response");
  }

  async createAgentIdentity(input: {
    operatorSlug: string;
    agentSlug: string;
  }): Promise<{
    ok: boolean;
    operatorSlug: string;
    agentSlug: string;
    identityType: "agent";
    declaredOperatorPublicKey: string;
  }> {
    const result = await this.request("createAgentIdentity", normalizeCreateAgentIdentityInput(input));
    return normalizeAgentIdentityStatus(result);
  }

  async openAgentSession(input: Record<string, unknown>): Promise<CoreSessionHandle> {
    const result = await this.request("openAgentSession", {
      agentSlug: sessionSlugFromInput(input),
    });
    return requireString(requireResponseRecord(result, "openAgentSession response"), "handle");
  }

  async syncOnce(handle: CoreSessionHandle): Promise<unknown> {
    return this.request("syncOnce", { handle: requireSessionHandle(handle) });
  }

  async processPendingMessages(handle: CoreSessionHandle): Promise<OpenedAgentMessage[]> {
    const result = await this.request(
      "processPendingMessages",
      { handle: requireSessionHandle(handle) },
    );
    const response = requireResponseRecord(result, "processPendingMessages response");
    if (!Array.isArray(response.messages)) {
      throw new Error("processPendingMessages response.messages is required");
    }
    return response.messages as OpenedAgentMessage[];
  }

  async sendChannelReply(
    handle: CoreSessionHandle,
    input: Record<string, unknown>,
  ): Promise<MessageRef> {
    validateRecordInput(input, "sendChannelReply");
    const result = await this.request("sendChannelReply", {
      handle: requireSessionHandle(handle),
      spaceId: requireString(input, "spaceId"),
      channelId: requireString(input, "channelId"),
      body: requireString(input, "body"),
      ...(typeof input.threadRootId === "string" ? { threadRootId: input.threadRootId } : {}),
      ...(typeof input.replyToId === "string" ? { replyToId: input.replyToId } : {}),
    });
    return { messageId: requireString(requireResponseRecord(result, "sendChannelReply response"), "messageId") };
  }

  async sendDirectReply(
    handle: CoreSessionHandle,
    input: Record<string, unknown>,
  ): Promise<MessageRef> {
    validateRecordInput(input, "sendDirectReply");
    const result = await this.request("sendDirectReply", {
      handle: requireSessionHandle(handle),
      recipientSlug: requireCoreSlug(input.recipientSlug, "recipientSlug"),
      body: requireString(input, "body"),
      ...(typeof input.replyToId === "string" ? { replyToId: input.replyToId } : {}),
    });
    return { messageId: requireString(requireResponseRecord(result, "sendDirectReply response"), "messageId") };
  }

  async snapshot(handle: CoreSessionHandle): Promise<unknown> {
    return this.request("snapshot", { handle: requireSessionHandle(handle) });
  }

  async closeSession(handle: CoreSessionHandle): Promise<void> {
    await this.request("closeSession", { handle: requireSessionHandle(handle) });
  }

  async devInjectChannelMessage(input: {
    senderSlug: string;
    agentSlug: string;
    body: string;
  }): Promise<DevInjectedChannelMessage> {
    validateRecordInput(input, "devInjectChannelMessage");
    const result = await this.request("devInjectChannelMessage", {
      senderSlug: requireCoreSlug(input.senderSlug, "senderSlug"),
      agentSlug: requireCoreSlug(input.agentSlug, "agentSlug"),
      body: requireString(input, "body"),
    });
    const response = requireResponseRecord(result, "devInjectChannelMessage response");
    return {
      messageId: requireString(response, "messageId"),
      spaceId: requireString(response, "spaceId"),
      channelId: requireString(response, "channelId"),
    };
  }

  async shutdown(): Promise<void> {
    const child = this.child;
    if (!child) return;
    this.child = undefined;
    if (child.exitCode !== null || child.signalCode !== null) return;
    await new Promise<void>((resolve) => {
      let timer: NodeJS.Timeout | undefined;
      child.once("exit", () => {
        if (timer) clearTimeout(timer);
        resolve();
      });
      child.kill("SIGTERM");
      timer = setTimeout(() => {
        if (child.exitCode === null && child.signalCode === null) child.kill("SIGKILL");
      }, 250);
      timer.unref?.();
    });
  }

  private request<T>(method: string, params: Record<string, unknown> = {}): Promise<T> {
    const child = this.ensureStarted();
    const id = this.nextId++;
    return new Promise<T>((resolve, reject) => {
      const timeout = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`native sidecar request timed out: ${method}`));
      }, this.requestTimeoutMs);
      timeout.unref?.();
      this.pending.set(id, {
        resolve: (value: unknown) => {
          clearTimeout(timeout);
          resolve(value as T);
        },
        reject: (error: Error) => {
          clearTimeout(timeout);
          reject(error);
        },
      });
      child.stdin.write(`${JSON.stringify({ id, method, params })}\n`, (error) => {
        if (error) {
          const pending = this.pending.get(id);
          this.pending.delete(id);
          if (pending) pending.reject(error);
        }
      });
    });
  }

  private ensureStarted(): ChildProcessWithoutNullStreams {
    if (this.child) return this.child;
    const launch = sidecarLaunchCommand({
      manifestPath: this.manifestPath,
      features: this.features,
      ...(this.binaryPath ? { binaryPath: this.binaryPath } : {}),
    });
    if (launch.diagnostic) throw new Error(launch.diagnostic);
    const child = spawn(launch.command, launch.args, {
      env: sidecarProcessEnv(process.env, this.stateHome),
      windowsHide: true,
    });
    this.child = child;

    const lines = readline.createInterface({ input: child.stdout });
    lines.on("line", (line) => this.handleLine(line));
    child.stderr.on("data", (chunk) => {
      this.stderrTail = `${this.stderrTail}${String(chunk)}`.slice(-8_192);
    });
    child.on("error", (error) => this.rejectAll(error));
    child.on("exit", (code, signal) => {
      if (this.child === child) this.child = undefined;
      const detail = this.stderrTail.trim();
      const reason = detail || `native sidecar exited code=${code ?? "null"} signal=${signal ?? "null"}`;
      this.rejectAll(new Error(reason));
    });

    return child;
  }

  private handleLine(line: string): void {
    let response: RpcResponse<unknown>;
    try {
      response = JSON.parse(line) as RpcResponse<unknown>;
    } catch {
      return;
    }
    if (!Number.isSafeInteger(response.id)) return;
    const pending = this.pending.get(response.id);
    if (!pending) return;
    this.pending.delete(response.id);
    if (typeof response.ok !== "boolean") {
      pending.reject(new Error("native sidecar response ok must be boolean"));
      return;
    }
    if (response.ok) {
      pending.resolve(response.result);
    } else {
      pending.reject(new Error(typeof response.error === "string" && response.error.trim()
        ? response.error
        : "native sidecar request failed"));
    }
  }

  private rejectAll(error: Error): void {
    for (const [, pending] of this.pending) {
      pending.reject(error);
    }
    this.pending.clear();
  }
}

export function sidecarLaunchCommand(options: SidecarLaunchOptions): SidecarLaunchCommand {
  if (options.binaryPath) {
    if (!path.isAbsolute(options.binaryPath)) {
      return {
        command: options.binaryPath,
        args: [],
        diagnostic: "native sidecar binary path must be absolute",
      };
    }
    if (!fs.existsSync(options.binaryPath)) {
      return {
        command: options.binaryPath,
        args: [],
        diagnostic: `native sidecar binary does not exist: ${options.binaryPath}`,
      };
    }
    return { command: options.binaryPath, args: [] };
  }
  if (!fs.existsSync(options.manifestPath)) {
    return {
      command: "agent-native-sidecar",
      args: [],
      diagnostic: [
        `native sidecar binary is unavailable for ${process.platform}/${process.arch}`,
        "Install a package that includes this platform binary or set AGENT_CORE_SIDECAR_BIN to an absolute sidecar path.",
      ].join(". "),
    };
  }
  return {
    command: "cargo",
    args: [
      "run",
      "--quiet",
      "--manifest-path",
      options.manifestPath,
      ...(options.features.length ? ["--features", options.features.join(",")] : []),
      "--bin",
      "agent-native-sidecar",
    ],
  };
}

function defaultSidecarBinaryPath(): string | undefined {
  const packageRoot = path.resolve(path.dirname(defaultManifestPath()), "../..");
  const executable = process.platform === "win32" ? "agent-native-sidecar.exe" : "agent-native-sidecar";
  const candidate = path.join(packageRoot, "bin", process.platform, process.arch, executable);
  return fs.existsSync(candidate) ? candidate : undefined;
}

function sidecarProcessEnv(source: NodeJS.ProcessEnv = process.env, stateHome?: string): NodeJS.ProcessEnv {
  const keys = [
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "TMPDIR",
    "TMP",
    "TEMP",
    "RUST_BACKTRACE",
    "RUST_LOG",
    "CARGO_HOME",
    "RUSTUP_HOME",
    "CARGO_TARGET_DIR",
    "CARGO_TERM_COLOR",
    "AGENT_CORE_NATIVE_PROFILE",
    "AGENT_CORE_SERVER_URL",
    "AGENT_CORE_DATABASE_PATH",
    "AGENT_CORE_AUTH_TOKEN",
  ];
  if (process.platform === "win32") {
    keys.push("SystemRoot", "ComSpec", "PATHEXT", "APPDATA", "LOCALAPPDATA", "USERPROFILE");
  }
  const env = Object.fromEntries(
    keys
      .map((key) => [key, source[key]] as const)
      .filter((entry): entry is readonly [string, string] => typeof entry[1] === "string"),
  );
  env.AGENT_CORE_SERVER_URL = source.AGENT_CORE_SERVER_URL?.trim() || DEFAULT_SERVER_URL;
  env.AGENT_CORE_DATABASE_PATH =
    source.AGENT_CORE_DATABASE_PATH?.trim() || path.join(stateHome?.trim() || defaultStateHome(source), "core.sqlite");
  return env;
}

function normalizeStartPairingInput(input: Record<string, unknown>): Record<string, unknown> {
  if (!input || typeof input !== "object" || Array.isArray(input)) {
    throw new Error("native startPairing body must be an object");
  }
  const unsupported = Object.keys(input).find((key) => key !== "pairingPublicNonce");
  if (unsupported) throw new Error(`${unsupported} is not supported for native startPairing`);
  if (input.pairingPublicNonce === undefined) return {};
  if (!isValidPairingPublicNonce(input.pairingPublicNonce)) {
    throw new Error("pairingPublicNonce must be an unpadded base64url string from 32 to 128 characters");
  }
  return { pairingPublicNonce: input.pairingPublicNonce };
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

function normalizeAgentIdentityStatus(value: unknown): {
  ok: boolean;
  operatorSlug: string;
  agentSlug: string;
  identityType: "agent";
  declaredOperatorPublicKey: string;
} {
  const response = requireResponseRecord(value, "createAgentIdentity response");
  if (response.ok !== true) {
    throw new Error("createAgentIdentity response.ok must be true");
  }
  const operatorSlug = requireCoreSlug(response.operatorSlug, "operatorSlug");
  const agentSlug = requireCoreSlug(response.agentSlug, "agentSlug");
  if (response.identityType !== "agent") {
    throw new Error("createAgentIdentity response.identityType must be agent");
  }
  return {
    ok: true,
    operatorSlug,
    agentSlug,
    identityType: "agent",
    declaredOperatorPublicKey: requireDeclaredOperatorPublicKey(response.declaredOperatorPublicKey, "declaredOperatorPublicKey"),
  };
}

function requireDeclaredOperatorPublicKey(value: unknown, field: string): string {
  const normalized = normalizeDeclaredOperatorPublicKey(value);
  if (!normalized) {
    throw new Error(`${field} is required and must be 4096 characters or less`);
  }
  return normalized;
}

function normalizeDeviceStatus(value: unknown, label: string): DeviceStatus {
  const response = requireResponseRecord(value, label);
  if (typeof response.connected !== "boolean") {
    throw new Error(`${label}.connected must be boolean`);
  }
  if (
    response.status !== "ready" &&
    response.status !== "unavailable" &&
    response.status !== "pairing_required"
  ) {
    throw new Error(`${label}.status must be ready, unavailable, or pairing_required`);
  }
  requireOptionalString(response, "deviceId", label);
  requireOptionalString(response, "reason", label);
  requireOptionalString(response, "nextAction", label);
  requireOptionalString(response, "serverUrl", label);
  requireOptionalStringArray(response, "blockedBy", label);
  requireOptionalStringArray(response, "missingConfig", label);
  if (
    response.authTokenSource !== undefined &&
    response.authTokenSource !== "env" &&
    response.authTokenSource !== "keychain" &&
    response.authTokenSource !== "memory"
  ) {
    throw new Error(`${label}.authTokenSource must be env, keychain, or memory`);
  }
  return response as unknown as DeviceStatus;
}

function normalizePairingStatus(value: unknown, label: string): PairingStatus {
  const response = requireResponseRecord(value, label);
  if (
    response.status !== "pending" &&
    response.status !== "confirmed" &&
    response.status !== "expired" &&
    response.status !== "canceled" &&
    response.status !== "unavailable"
  ) {
    throw new Error(`${label}.status must be pending, confirmed, expired, canceled, or unavailable`);
  }
  for (const key of ["pairingId", "userCode", "confirmUrl", "expiresAt", "operatorSlug", "accountId", "reason", "nextAction"]) {
    requireOptionalString(response, key, label);
  }
  requireOptionalStringArray(response, "blockedBy", label);
  if (response.pollAfterMs !== undefined && (typeof response.pollAfterMs !== "number" || !Number.isFinite(response.pollAfterMs))) {
    throw new Error(`${label}.pollAfterMs must be a finite number`);
  }
  if (response.core !== undefined) normalizeDeviceStatus(response.core, `${label}.core`);
  return response as unknown as PairingStatus;
}

function requireString(input: Record<string, unknown>, key: string): string {
  const value = input[key];
  if (typeof value !== "string" || !value.trim()) {
    throw new Error(`${key} is required`);
  }
  return value;
}

function requireOptionalString(input: Record<string, unknown>, key: string, label: string): void {
  if (input[key] !== undefined && typeof input[key] !== "string") {
    throw new Error(`${label}.${key} must be a string`);
  }
}

function requireOptionalStringArray(input: Record<string, unknown>, key: string, label: string): void {
  const value = input[key];
  if (value === undefined) return;
  if (!Array.isArray(value) || !value.every((item) => typeof item === "string")) {
    throw new Error(`${label}.${key} must be an array of strings`);
  }
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
