import crypto from "node:crypto";
import fs from "node:fs/promises";
import path from "node:path";
import { assertSafeAgentId, isSafeAgentId } from "../platform/agent-id.js";
import { normalizeDeclaredOperatorPublicKey } from "../platform/core-identity.js";
import { isValidCoreSlug } from "../platform/core-slug.js";
import { assertDirectoryWithinRoot, assertSafeRegularFile, ensureDirectory, isNotFound } from "../platform/safe-directory.js";
import { AgentConfig, AgentCreateInput, CoreAgentIdentity } from "../types.js";
import { parseAccessMode } from "../policy/access-mode.js";
import { normalizeDeniedTools } from "../policy/denied-tools.js";
import { normalizeFileAccess } from "../policy/file-access.js";
import { defaultNetworkAccess, parseNetworkAccess } from "../policy/network-access.js";
import { normalizeProviderConfigPathsForProvider } from "../policy/provider-config-paths.js";
import { agentDir, agentWorkspace, statePaths, StatePaths } from "./paths.js";

export interface DaemonState {
  pid: number;
  host: string;
  port: number;
  startedAt: string;
  instanceId?: string;
}

export interface DeviceState {
  apiToken: string;
  createdAt: string;
  updatedAt: string;
  localGrants?: LocalAccessGrant[];
  binding?: DeviceBinding;
}

export type LocalAccessScope = "management";

export interface DeviceBinding {
  accountId: string;
  operatorSlug: string;
  pairedAt: string;
  deviceId?: string;
  pairingId?: string;
}

export interface DeviceBindingInput {
  accountId: string;
  operatorSlug: string;
  deviceId?: string;
  pairingId?: string;
}

export interface RequestBindingInput {
  accountId?: string;
  operatorSlug?: string;
}

export interface LocalAccessGrant {
  id: string;
  tokenHash: string;
  scopes: LocalAccessScope[];
  createdAt: string;
  expiresAt: string;
  accountId?: string;
  operatorSlug?: string;
  revokedAt?: string;
}

export interface CreatedLocalAccessGrant {
  id: string;
  token: string;
  scopes: LocalAccessScope[];
  expiresAt: string;
  accountId?: string;
  operatorSlug?: string;
}

export interface LocalAccessGrantSummary {
  id: string;
  scopes: LocalAccessScope[];
  createdAt: string;
  expiresAt: string;
  accountId?: string;
  operatorSlug?: string;
  revokedAt?: string;
  active: boolean;
}

export class StateStore {
  readonly paths: StatePaths;

  constructor(root?: string) {
    this.paths = statePaths(root);
  }

  async init(): Promise<void> {
    await ensureDirectory(this.paths.root);
    await ensureDirectory(this.paths.agentsDir, { root: this.paths.root });
  }

  async ensureDeviceState(): Promise<DeviceState> {
    await this.init();
    const existing = await this.readDeviceState();
    if (existing?.apiToken) {
      await fs.chmod(this.paths.deviceFile, 0o600).catch(() => undefined);
      return existing;
    }
    const now = new Date().toISOString();
    const device: DeviceState = {
      apiToken: crypto.randomBytes(32).toString("base64url"),
      createdAt: now,
      updatedAt: now,
    };
    await this.writeJson(this.paths.deviceFile, device);
    return device;
  }

  async readDeviceState(): Promise<DeviceState | undefined> {
    try {
      return await this.readJson<DeviceState>(this.paths.deviceFile);
    } catch (error) {
      if (error instanceof SyntaxError) return undefined;
      throw error;
    }
  }

  async readDeviceBinding(): Promise<DeviceBinding | undefined> {
    const device = await this.readDeviceState();
    return safeDeviceBinding(device?.binding);
  }

  async setDeviceBinding(
    input: DeviceBindingInput,
    options: { clearLocalGrants?: boolean } = {},
  ): Promise<DeviceBinding> {
    const device = await this.ensureDeviceState();
    const now = new Date().toISOString();
    const existing = safeDeviceBinding(device.binding);
    const binding: DeviceBinding = {
      accountId: requireBoundedString(input.accountId, "accountId", 256),
      operatorSlug: requireCoreSlug(input.operatorSlug, "operatorSlug"),
      pairedAt: existing?.accountId === input.accountId && existing?.operatorSlug === input.operatorSlug
        ? existing.pairedAt
        : now,
      ...(input.deviceId !== undefined
        ? { deviceId: requireBoundedString(input.deviceId, "deviceId", 256) }
        : existing?.deviceId ? { deviceId: existing.deviceId } : {}),
      ...(input.pairingId !== undefined
        ? { pairingId: requireBoundedString(input.pairingId, "pairingId", 256) }
        : existing?.pairingId ? { pairingId: existing.pairingId } : {}),
    };
    await this.saveDeviceState({
      ...device,
      binding,
      ...(options.clearLocalGrants ? { localGrants: [] } : {}),
      updatedAt: now,
    });
    return binding;
  }

  async createLocalAccessGrant(
    input: { scopes?: LocalAccessScope[]; ttlMs?: number; binding?: RequestBindingInput } = {},
  ): Promise<CreatedLocalAccessGrant> {
    const device = await this.ensureDeviceState();
    const now = new Date();
    const token = crypto.randomBytes(32).toString("base64url");
    const binding = safeRequestBinding(input.binding) ?? safeDeviceBinding(device.binding);
    const grant: LocalAccessGrant = {
      id: crypto.randomUUID(),
      tokenHash: hashLocalAccessToken(token),
      scopes: normalizeLocalAccessScopes(input.scopes),
      createdAt: now.toISOString(),
      expiresAt: new Date(now.getTime() + normalizeGrantTtl(input.ttlMs)).toISOString(),
      ...(binding?.accountId ? { accountId: binding.accountId } : {}),
      ...(binding?.operatorSlug ? { operatorSlug: binding.operatorSlug } : {}),
    };
    await this.saveDeviceState({
      ...device,
      localGrants: [...activeOrFutureGrants(safeLocalGrants(device.localGrants), now), grant],
      updatedAt: now.toISOString(),
    });
    return {
      id: grant.id,
      token,
      scopes: grant.scopes,
      expiresAt: grant.expiresAt,
      ...(grant.accountId ? { accountId: grant.accountId } : {}),
      ...(grant.operatorSlug ? { operatorSlug: grant.operatorSlug } : {}),
    };
  }

  async rotateLocalControlToken(): Promise<string> {
    const device = await this.ensureDeviceState();
    const now = new Date().toISOString();
    const token = crypto.randomBytes(32).toString("base64url");
    await this.saveDeviceState({
      ...device,
      apiToken: token,
      localGrants: [],
      updatedAt: now,
    });
    return token;
  }

  async revokeLocalAccessGrant(id: string): Promise<boolean> {
    const device = await this.readDeviceState();
    const grants = safeLocalGrants(device?.localGrants);
    if (!device || grants.length === 0) return false;
    const now = new Date().toISOString();
    let changed = false;
    const localGrants = grants.map((grant) => {
      if (grant.id !== id || grant.revokedAt) return grant;
      changed = true;
      return { ...grant, revokedAt: now };
    });
    if (!changed) return false;
    await this.saveDeviceState({ ...device, localGrants, updatedAt: now });
    return true;
  }

  async listLocalAccessGrants(): Promise<LocalAccessGrantSummary[]> {
    const device = await this.readDeviceState();
    const now = new Date();
    return safeLocalGrants(device?.localGrants).map((grant) => ({
      id: grant.id,
      scopes: grant.scopes,
      createdAt: grant.createdAt,
      expiresAt: grant.expiresAt,
      ...(grant.accountId ? { accountId: grant.accountId } : {}),
      ...(grant.operatorSlug ? { operatorSlug: grant.operatorSlug } : {}),
      ...(grant.revokedAt ? { revokedAt: grant.revokedAt } : {}),
      active: !grant.revokedAt && Date.parse(grant.expiresAt) > now.getTime(),
    }));
  }

  async verifyLocalAccessGrant(
    token: string,
    scope: LocalAccessScope = "management",
    requestBinding?: RequestBindingInput,
  ): Promise<boolean> {
    const device = await this.readDeviceState();
    const grants = safeLocalGrants(device?.localGrants);
    if (!grants.length) return false;
    const deviceBinding = safeDeviceBinding(device?.binding);
    const presentedBinding = safeRequestBinding(requestBinding);
    if (deviceBinding && !requestBindingMatches(presentedBinding, deviceBinding)) return false;
    const tokenHash = hashLocalAccessToken(token);
    const now = new Date();
    return grants.some((grant) => {
      if (grant.revokedAt) return false;
      if (!grant.scopes.includes(scope)) return false;
      if (Date.parse(grant.expiresAt) <= now.getTime()) return false;
      if (!grantBindingMatchesRequest(grant, presentedBinding)) return false;
      return timingSafeEqualString(grant.tokenHash, tokenHash);
    });
  }

  async saveDaemon(state: DaemonState): Promise<void> {
    await this.writeJson(this.paths.daemonFile, state);
  }

  async readDaemon(): Promise<DaemonState | undefined> {
    try {
      return await this.readJson<DaemonState>(this.paths.daemonFile);
    } catch (error) {
      if (error instanceof SyntaxError) return undefined;
      throw error;
    }
  }

  async clearDaemon(): Promise<void> {
    await fs.rm(this.paths.daemonFile, { force: true });
  }

  async listAgents(): Promise<AgentConfig[]> {
    await this.init();
    const entries = await fs.readdir(this.paths.agentsDir, { withFileTypes: true });
    const agents = await Promise.all(
      entries
        .filter((entry) => entry.isDirectory())
        .filter((entry) => isSafeAgentId(entry.name))
        .map((entry) => this.getAgentForList(entry.name)),
    );
    return agents.filter((agent): agent is AgentConfig => Boolean(agent));
  }

  async getAgent(id: string): Promise<AgentConfig | undefined> {
    assertSafeAgentId(id);
    const agent = await this.readJson<AgentConfig>(path.join(agentDir(this.paths.root, id), "agent.json"));
    return agent ? normalizeAgentConfig(agent) : undefined;
  }

  async deleteAgent(id: string): Promise<void> {
    assertSafeAgentId(id);
    const dir = agentDir(this.paths.root, id);
    try {
      await assertDirectoryWithinRoot(dir, this.paths.root);
    } catch (error) {
      if (isNotFound(error)) return;
      throw error;
    }
    await fs.rm(dir, { recursive: true, force: true });
  }

  async createAgent(input: AgentCreateInput): Promise<AgentConfig> {
    const id = crypto.randomUUID();
    const now = new Date().toISOString();
    const dir = agentDir(this.paths.root, id);
    const workspace = agentWorkspace(this.paths.root, id);
    const accessMode = parseAccessMode(input.accessMode);
    const networkAccess = parseNetworkAccess(
      input.networkAccess,
      accessMode === "trusted" ? "inherit" : defaultNetworkAccess(),
    );
    await ensureDirectory(workspace, { root: this.paths.root });
    const agent: AgentConfig = {
      id,
      name: input.name,
      provider: input.provider,
      accessMode,
      networkAccess,
      deniedTools: normalizeDeniedTools(input.deniedTools),
      fileAccess: normalizeFileAccess(input.fileAccess),
      providerConfigPaths: normalizeProviderConfigPathsForProvider(input.provider, input.providerConfigPaths),
      instructions: input.instructions ?? "",
      workspace,
      status: "created",
      createdAt: now,
      updatedAt: now,
    };
    if (input.projectPath) agent.projectPath = input.projectPath;
    if (input.coreIdentity) agent.coreIdentity = normalizeCoreIdentity(input.coreIdentity);
    await this.writeJson(path.join(dir, "agent.json"), agent);
    await this.writeJson(path.join(dir, "runtime.json"), { provider: agent.provider });
    await this.writeJson(path.join(dir, "session.json"), {});
    return agent;
  }

  async saveAgent(agent: AgentConfig): Promise<void> {
    assertSafeAgentId(agent.id);
    normalizeAgentConfig(agent);
    agent.updatedAt = new Date().toISOString();
    await this.writeJson(path.join(agentDir(this.paths.root, agent.id), "agent.json"), agent);
  }

  async readSession<T>(agentId: string): Promise<T | undefined> {
    assertSafeAgentId(agentId);
    return this.readJson<T>(path.join(agentDir(this.paths.root, agentId), "session.json"));
  }

  async saveSession<T>(agentId: string, session: T): Promise<void> {
    assertSafeAgentId(agentId);
    await this.writeJson(path.join(agentDir(this.paths.root, agentId), "session.json"), session);
  }

  private async getAgentForList(id: string): Promise<AgentConfig | undefined> {
    try {
      return await this.getAgent(id);
    } catch {
      return undefined;
    }
  }

  private async readJson<T>(file: string): Promise<T | undefined> {
    try {
      await assertDirectoryWithinRoot(path.dirname(file), this.paths.root);
      await assertSafeRegularFile(file);
      return JSON.parse(await fs.readFile(file, "utf8")) as T;
    } catch (error) {
      if (isNotFound(error)) return undefined;
      throw error;
    }
  }

  private async writeJson(file: string, value: unknown): Promise<void> {
    const dir = path.dirname(file);
    await ensureDirectory(dir, { root: this.paths.root });
    const tmp = `${file}.${process.pid}.${Date.now()}.${crypto.randomUUID()}.tmp`;
    await fs.writeFile(tmp, `${JSON.stringify(value, null, 2)}\n`, { encoding: "utf8", mode: 0o600 });
    await fs.rename(tmp, file);
    await fs.chmod(file, 0o600).catch(() => undefined);
  }

  private async saveDeviceState(device: DeviceState): Promise<void> {
    await this.writeJson(this.paths.deviceFile, device);
  }
}

function normalizeAgentConfig(agent: AgentConfig): AgentConfig {
  agent.accessMode = parseAccessMode(agent.accessMode);
  agent.networkAccess = parseNetworkAccess(
    agent.networkAccess,
    agent.accessMode === "trusted" ? "inherit" : defaultNetworkAccess(),
  );
  agent.deniedTools = normalizeDeniedTools(agent.deniedTools);
  agent.fileAccess = normalizeFileAccess(agent.fileAccess);
  agent.providerConfigPaths = normalizeProviderConfigPathsForProvider(agent.provider, agent.providerConfigPaths);
  if (agent.coreIdentity) agent.coreIdentity = normalizeCoreIdentity(agent.coreIdentity);
  return agent;
}

function normalizeCoreIdentity(identity: CoreAgentIdentity): CoreAgentIdentity;
function normalizeCoreIdentity(identity: undefined): undefined;
function normalizeCoreIdentity(identity: AgentConfig["coreIdentity"]): AgentConfig["coreIdentity"] {
  if (!identity) return undefined;
  const declaredOperatorPublicKey = normalizeDeclaredOperatorPublicKey(identity.declaredOperatorPublicKey);
  if (!declaredOperatorPublicKey) {
    return undefined;
  }
  if (!isValidCoreSlug(identity.operatorSlug) || !isValidCoreSlug(identity.agentSlug)) {
    return undefined;
  }
  if (identity.identityType !== "agent") {
    return undefined;
  }
  return {
    operatorSlug: identity.operatorSlug,
    agentSlug: identity.agentSlug,
    identityType: "agent",
    declaredOperatorPublicKey,
    source: identity.source === "native" ? "native" : "web_signed",
  };
}

function normalizeLocalAccessScopes(scopes: LocalAccessScope[] | undefined): LocalAccessScope[] {
  const normalized = scopes?.filter((scope) => scope === "management") ?? ["management"];
  return normalized.length ? Array.from(new Set(normalized)) : ["management"];
}

function normalizeGrantTtl(ttlMs: number | undefined): number {
  const oneHour = 60 * 60 * 1000;
  const thirtyDays = 30 * 24 * oneHour;
  if (typeof ttlMs !== "number" || !Number.isFinite(ttlMs)) return oneHour;
  return Math.max(0, Math.min(thirtyDays, Math.floor(ttlMs)));
}

function safeDeviceBinding(value: unknown): DeviceBinding | undefined {
  if (!value || typeof value !== "object" || Array.isArray(value)) return undefined;
  const input = value as Partial<DeviceBinding>;
  if (typeof input.accountId !== "string" || typeof input.operatorSlug !== "string") return undefined;
  if (typeof input.pairedAt !== "string" || !Number.isFinite(Date.parse(input.pairedAt))) return undefined;
  if (!isValidCoreSlug(input.operatorSlug)) return undefined;
  const accountId = input.accountId.trim();
  if (!accountId || accountId.length > 256) return undefined;
  return {
    accountId,
    operatorSlug: input.operatorSlug,
    pairedAt: input.pairedAt,
    ...(typeof input.deviceId === "string" && input.deviceId.trim() && input.deviceId.length <= 256
      ? { deviceId: input.deviceId.trim() }
      : {}),
    ...(typeof input.pairingId === "string" && input.pairingId.trim() && input.pairingId.length <= 256
      ? { pairingId: input.pairingId.trim() }
      : {}),
  };
}

function safeRequestBinding(value: RequestBindingInput | undefined): RequestBindingInput | undefined {
  if (!value) return undefined;
  const accountId = typeof value.accountId === "string" ? value.accountId.trim() : undefined;
  const operatorSlug = typeof value.operatorSlug === "string" ? value.operatorSlug.trim() : undefined;
  const normalized: RequestBindingInput = {};
  if (accountId && accountId.length <= 256) normalized.accountId = accountId;
  if (operatorSlug && isValidCoreSlug(operatorSlug)) normalized.operatorSlug = operatorSlug;
  return normalized.accountId || normalized.operatorSlug ? normalized : undefined;
}

function requestBindingMatches(request: RequestBindingInput | undefined, binding: DeviceBinding): boolean {
  if (!request?.accountId && !request?.operatorSlug) return false;
  if (request.accountId !== undefined && request.accountId !== binding.accountId) return false;
  if (request.operatorSlug !== undefined && request.operatorSlug !== binding.operatorSlug) return false;
  return true;
}

function grantBindingMatchesRequest(
  grant: Pick<LocalAccessGrant, "accountId" | "operatorSlug">,
  request: RequestBindingInput | undefined,
): boolean {
  if (grant.accountId === undefined && grant.operatorSlug === undefined) return true;
  if (!request) return false;
  if (grant.accountId !== undefined && request.accountId !== grant.accountId) return false;
  if (grant.operatorSlug !== undefined && request.operatorSlug !== grant.operatorSlug) return false;
  return true;
}

function requireBoundedString(value: string, label: string, maxLength: number): string {
  if (typeof value !== "string" || !value.trim() || value.trim().length > maxLength) {
    throw new Error(`${label} must be a non-empty string up to ${maxLength} characters`);
  }
  return value.trim();
}

function requireCoreSlug(value: string, label: string): string {
  if (!isValidCoreSlug(value)) throw new Error(`${label} must be a lowercase slug`);
  return value;
}

function activeOrFutureGrants(grants: LocalAccessGrant[] | undefined, now: Date): LocalAccessGrant[] {
  return (grants ?? []).filter((grant) => !grant.revokedAt && Date.parse(grant.expiresAt) > now.getTime());
}

function safeLocalGrants(grants: unknown): LocalAccessGrant[] {
  if (!Array.isArray(grants)) return [];
  return grants.filter((grant): grant is LocalAccessGrant => {
    if (!grant || typeof grant !== "object") return false;
    const candidate = grant as Partial<LocalAccessGrant>;
    return (
      typeof candidate.id === "string" &&
      typeof candidate.tokenHash === "string" &&
      Array.isArray(candidate.scopes) &&
      candidate.scopes.every((scope) => scope === "management") &&
      typeof candidate.createdAt === "string" &&
      typeof candidate.expiresAt === "string" &&
      (candidate.accountId === undefined || typeof candidate.accountId === "string") &&
      (candidate.operatorSlug === undefined || typeof candidate.operatorSlug === "string") &&
      (candidate.revokedAt === undefined || typeof candidate.revokedAt === "string")
    );
  });
}

function hashLocalAccessToken(token: string): string {
  return crypto.createHash("sha256").update(token).digest("base64url");
}

function timingSafeEqualString(left: string, right: string): boolean {
  const leftBuffer = Buffer.from(left);
  const rightBuffer = Buffer.from(right);
  if (leftBuffer.length !== rightBuffer.length) return false;
  return crypto.timingSafeEqual(leftBuffer, rightBuffer);
}
