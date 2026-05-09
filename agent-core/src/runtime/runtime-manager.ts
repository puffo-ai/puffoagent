import crypto from "node:crypto";
import { LogStore } from "../logs/log-store.js";
import { redact } from "../logs/redact.js";
import { CoreNative, CoreSessionHandle, DevInjectedChannelMessage } from "../native/core.js";
import { PolicyResolver, PolicyResolutionError } from "../policy/policy-resolver.js";
import { parseAccessMode } from "../policy/access-mode.js";
import { normalizeDeniedTools } from "../policy/denied-tools.js";
import { defaultNetworkAccess, parseNetworkAccess } from "../policy/network-access.js";
import { ResolvedPolicy } from "../policy/policy.js";
import { mergeFileAccess, normalizeFileAccess } from "../policy/file-access.js";
import { mergeProviderConfigPaths, normalizeProviderConfigPathsForProvider } from "../policy/provider-config-paths.js";
import { assertSandboxAvailable } from "../policy/sandbox.js";
import { createProviderSession, ProviderSessionState } from "../providers/provider-session.js";
import { ProviderSession } from "../providers/provider-types.js";
import { StateStore } from "../state/store.js";
import { agentWorkspace } from "../state/paths.js";
import { AgentConfig, AgentCreateInput, AgentPolicyUpdateInput, CoreAgentIdentity, ProviderStatus } from "../types.js";
import { MessageLoop } from "../messaging/message-loop.js";
import { resolveExecutablePath } from "../platform/shell.js";
import { normalizeDeclaredOperatorPublicKey } from "../platform/core-identity.js";
import { isValidCoreSlug } from "../platform/core-slug.js";
import { AgentRuntime } from "./agent-runtime.js";
import { RuntimeSupervisor } from "./runtime-supervisor.js";

export type ProviderSessionFactory = (
  provider: AgentConfig["provider"],
  policy: ResolvedPolicy,
  state: ProviderSessionState,
) => ProviderSession;

export interface RuntimeManagerOptions {
  providerFactory?: ProviderSessionFactory;
  messagePollMs?: number;
  autoStartMessageLoop?: boolean;
}

export interface DevInjectMessageInput {
  senderSlug?: string;
  body: string;
}

export interface DevInjectMessageResult {
  injected: DevInjectedChannelMessage;
  handled: number;
}

export interface AgentRuntimeStatusSnapshot {
  agent: AgentConfig;
  runtime: {
    attached: boolean;
    providerStatus?: ProviderStatus;
    coreSessionOpen: boolean;
    messageLoopOpen: boolean;
    pollerActive: boolean;
    tickInProgress: boolean;
  };
}

export interface AgentPolicySnapshot {
  agent: AgentConfig;
  policy: {
    accessMode: ResolvedPolicy["accessMode"];
    cwd: string;
    agentHome: string;
    workspace: string;
    projectPath?: string;
    networkAccess: ResolvedPolicy["networkAccess"];
    deniedTools: string[];
    fileAccess: ResolvedPolicy["fileAccess"];
    providerConfigPaths: string[];
    sandbox?: ResolvedPolicy["sandbox"];
  };
}

export class AgentNotFoundError extends Error {
  constructor(readonly agentId: string) {
    super(`agent not found: ${agentId}`);
    this.name = "AgentNotFoundError";
  }
}

export { PolicyResolutionError };

export class RuntimeManager {
  private readonly supervisor = new RuntimeSupervisor();
  private readonly policyResolver: PolicyResolver;
  private readonly providerFactory: ProviderSessionFactory;
  private readonly messagePollMs: number;
  private readonly autoStartMessageLoop: boolean;
  private readonly coreHandles = new Map<string, CoreSessionHandle>();
  private readonly messageLoops = new Map<string, MessageLoop>();
  private readonly pollTimers = new Map<string, NodeJS.Timeout>();
  private readonly ticking = new Set<string>();

  constructor(
    private readonly store: StateStore,
    private readonly logs: LogStore,
    private readonly core?: CoreNative,
    options: RuntimeManagerOptions = {},
  ) {
    this.policyResolver = new PolicyResolver(store);
    this.providerFactory = options.providerFactory ?? createProviderSession;
    this.messagePollMs = options.messagePollMs ?? 5_000;
    this.autoStartMessageLoop = options.autoStartMessageLoop ?? true;
  }

  async createAgent(input: AgentCreateInput): Promise<AgentConfig> {
    const createInput = normalizeAgentCreateInput(input);
    const agent = await this.store.createAgent(createInput);
    if (createInput.coreIdentity) {
      await this.logs.append(
        agent.id,
        `attached core agent identity slug=${createInput.coreIdentity.agentSlug} source=${createInput.coreIdentity.source ?? "web_signed"}`,
      );
    } else if (createInput.operatorSlug && this.core?.createAgentIdentity) {
      try {
        const identity = await this.core.createAgentIdentity({
          operatorSlug: createInput.operatorSlug,
          agentSlug: createInput.agentSlug || slugifyAgentName(createInput.name),
        });
        assertCoreIdentityShape(identity, "native core returned invalid agent identity");
        const declaredOperatorPublicKey = requireDeclaredOperatorPublicKey(identity.declaredOperatorPublicKey);
        agent.coreIdentity = {
          operatorSlug: identity.operatorSlug,
          agentSlug: identity.agentSlug,
          identityType: identity.identityType,
          source: "native",
          declaredOperatorPublicKey,
        };
        await this.store.saveAgent(agent);
        await this.logs.append(agent.id, `created core agent identity slug=${identity.agentSlug}`);
      } catch (error) {
        agent.status = "error";
        agent.lastError = errorMessage(error);
        await this.store.saveAgent(agent);
        await this.logs.append(agent.id, `failed to create core agent identity: ${agent.lastError}`);
        throw new Error(agent.lastError);
      }
    }
    await this.logs.append(agent.id, `created agent provider=${agent.provider} accessMode=${agent.accessMode}`);
    if (createInput.start) return this.startAgent(agent.id);
    return agent;
  }

  async listAgents(): Promise<AgentConfig[]> {
    return this.store.listAgents();
  }

  async getAgent(id: string): Promise<AgentConfig | undefined> {
    return this.store.getAgent(id);
  }

  async deleteAgent(id: string): Promise<{ id: string; deleted: true }> {
    const agent = await this.requireAgent(id);
    await this.stopAgentRuntime(agent);
    await this.logs.append(id, "deleted agent runtime");
    await this.store.deleteAgent(agent.id);
    return { id: agent.id, deleted: true };
  }

  async getAgentStatus(id: string): Promise<AgentRuntimeStatusSnapshot> {
    const agent = await this.requireAgent(id);
    const runtime = this.supervisor.get(id);
    return {
      agent,
      runtime: {
        attached: Boolean(runtime),
        ...(runtime ? { providerStatus: redactProviderStatus(runtime.getStatus()) } : {}),
        coreSessionOpen: this.coreHandles.has(id),
        messageLoopOpen: this.messageLoops.has(id),
        pollerActive: this.pollTimers.has(id),
        tickInProgress: this.ticking.has(id),
      },
    };
  }

  async getAgentPolicy(id: string): Promise<AgentPolicySnapshot> {
    const agent = await this.requireAgent(id);
    const policy = await this.policyResolver.preview(agent);
    return agentPolicySnapshot(agent, policy);
  }

  async previewCreateAgent(input: AgentCreateInput): Promise<AgentPolicySnapshot> {
    const agent = draftAgentConfig(normalizeAgentCreateInput(input), this.store.paths.root);
    const policy = await this.policyResolver.preview(agent);
    return agentPolicySnapshot(agent, policy);
  }

  async previewAgentPolicy(id: string, input: AgentPolicyUpdateInput): Promise<AgentPolicySnapshot> {
    const agent = applyPolicyUpdate(await this.requireAgent(id), input);
    const policy = await this.policyResolver.preview(agent);
    return agentPolicySnapshot(agent, policy);
  }

  async startAgent(id: string): Promise<AgentConfig> {
    const agent = await this.requireAgent(id);
    if (this.supervisor.get(id)) return agent;
    const policy = await this.policyResolver.resolve(agent);
    assertSandboxAvailable(policy);
    const sessionState = await this.ensureProviderSessionState(agent);
    const session = this.providerFactory(agent.provider, policy, {
      ...sessionState,
      ...(await this.resolveProviderCommandPath(agent, policy)),
    });
    const runtime = new AgentRuntime({ config: agent, providerSession: session });
    try {
      await runtime.start();
      this.supervisor.set(id, runtime);
      await this.openMessageLoop(agent, runtime);
      agent.status = "running";
      delete agent.lastError;
      await this.store.saveAgent(agent);
      await this.logs.append(id, "started agent runtime");
      return agent;
    } catch (error) {
      await runtime.stop().catch(() => undefined);
      this.supervisor.delete(id);
      await this.closeMessageLoop(id);
      agent.status = "error";
      agent.lastError = errorMessage(error);
      await this.store.saveAgent(agent);
      await this.logs.append(id, `failed to start agent runtime: ${agent.lastError}`);
      throw new Error(agent.lastError);
    }
  }

  private async ensureProviderSessionState(agent: AgentConfig): Promise<ProviderSessionState> {
    const existing = (await this.store.readSession<ProviderSessionState>(agent.id)) ?? {};
    if (agent.provider === "claude" && !existing.sessionId) {
      const next = { ...existing, sessionId: crypto.randomUUID() };
      await this.store.saveSession(agent.id, next);
      return next;
    }
    return existing;
  }

  private async resolveProviderCommandPath(
    agent: AgentConfig,
    policy: ResolvedPolicy,
  ): Promise<Pick<ProviderSessionState, "commandPath">> {
    const commandPath = await resolveExecutablePath(agent.provider, policy.env.PATH);
    return commandPath ? { commandPath } : {};
  }

  async stopAgent(id: string): Promise<AgentConfig> {
    const agent = await this.requireAgent(id);
    const stopError = await this.stopAgentRuntime(agent);
    agent.status = "stopped";
    if (stopError) agent.lastError = stopError;
    await this.store.saveAgent(agent);
    await this.logs.append(id, "stopped agent runtime");
    return agent;
  }

  async markAgentError(id: string, message: string): Promise<AgentConfig> {
    const agent = await this.requireAgent(id);
    await this.stopAgentRuntime(agent);
    agent.status = "error";
    agent.lastError = errorMessage(message);
    await this.store.saveAgent(agent);
    await this.logs.append(id, `agent error: ${agent.lastError}`);
    return agent;
  }

  private async stopAgentRuntime(agent: AgentConfig): Promise<string | undefined> {
    const id = agent.id;
    await this.closeMessageLoop(id);
    const runtime = this.supervisor.get(id);
    let stopError: string | undefined;
    if (runtime) {
      try {
        await runtime.stop();
      } catch (error) {
        stopError = errorMessage(error);
        await this.logs.append(id, `provider stop error: ${stopError}`);
      }
    }
    this.supervisor.delete(id);
    return stopError;
  }

  async restartAgent(id: string): Promise<AgentConfig> {
    await this.stopAgent(id);
    return this.startAgent(id);
  }

  async updateAgentPolicy(id: string, input: AgentPolicyUpdateInput): Promise<AgentConfig> {
    let agent = await this.requireAgent(id);
    const wasRunning = Boolean(this.supervisor.get(id));
    if (wasRunning) {
      await this.stopAgent(id);
      agent = await this.requireAgent(id);
    }

    agent = applyPolicyUpdate(agent, input);

    await this.store.saveAgent(agent);
    await this.logs.append(id, "updated agent policy");
    if (wasRunning) return this.startAgent(id);
    return agent;
  }

  async resetSession(id: string): Promise<AgentConfig> {
    const agent = await this.requireAgent(id);
    const wasRunning = Boolean(this.supervisor.get(id));
    if (wasRunning) await this.stopAgent(id);
    await this.store.saveSession(id, {});
    await this.logs.append(id, "reset provider session");
    if (wasRunning) return this.startAgent(id);
    return agent;
  }

  async tickAgent(id: string): Promise<number> {
    const loop = this.messageLoops.get(id);
    const handle = this.coreHandles.get(id);
    if (!loop || !handle || !this.core) return 0;
    if (this.ticking.has(id)) return 0;
    this.ticking.add(id);
    try {
      await this.core.syncOnce(handle);
      const handled = await loop.tick();
      if (handled > 0) {
        const agent = await this.store.getAgent(id);
        if (!agent) return handled;
        await this.persistProviderSessionState(id);
        agent.lastActiveAt = new Date().toISOString();
        delete agent.lastError;
        await this.store.saveAgent(agent);
        await this.logs.append(id, `processed ${handled} message(s)`);
      }
      return handled;
    } catch (error) {
      const agent = await this.store.getAgent(id);
      if (!agent) return 0;
      agent.lastError = errorMessage(error);
      await this.store.saveAgent(agent);
      await this.logs.append(id, `message loop error: ${agent.lastError}`);
      return 0;
    } finally {
      this.ticking.delete(id);
    }
  }

  async devInjectMessage(id: string, input: DevInjectMessageInput): Promise<DevInjectMessageResult> {
    if (!this.core?.devInjectChannelMessage) {
      throw new Error("dev message injection is not supported by this native core");
    }
    const agent = await this.requireAgent(id);
    const agentSlug = agent.coreIdentity?.agentSlug;
    if (!agentSlug) {
      throw new Error("agent does not have a core identity");
    }
    if (!this.supervisor.get(id)) {
      throw new Error("agent runtime must be running before injecting a dev message");
    }
    const injected = await this.core.devInjectChannelMessage({
      senderSlug: input.senderSlug || agent.coreIdentity?.operatorSlug || "dev-user",
      agentSlug,
      body: input.body,
    });
    const handled = await this.tickAgent(id);
    return { injected, handled };
  }

  private async requireAgent(id: string): Promise<AgentConfig> {
    const agent = await this.store.getAgent(id);
    if (!agent) throw new AgentNotFoundError(id);
    return agent;
  }

  private async openMessageLoop(agent: AgentConfig, runtime: AgentRuntime): Promise<void> {
    const agentSlug = agent.coreIdentity?.agentSlug;
    if (!this.core || !agentSlug) return;
    const handle = await this.core.openAgentSession({
      agentId: agent.id,
      agentSlug,
      provider: agent.provider,
    });
    this.coreHandles.set(agent.id, handle);
    this.messageLoops.set(agent.id, new MessageLoop(this.core, handle, agent, runtime));
    await this.logs.append(agent.id, `opened core session slug=${agentSlug}`);
    if (this.autoStartMessageLoop) this.startMessagePoller(agent.id);
  }

  private startMessagePoller(id: string): void {
    if (this.pollTimers.has(id)) return;
    const timer = setInterval(() => {
      void this.tickAgent(id);
    }, this.messagePollMs);
    timer.unref?.();
    this.pollTimers.set(id, timer);
    void this.tickAgent(id);
  }

  private async persistProviderSessionState(id: string): Promise<void> {
    const runtime = this.supervisor.get(id);
    const sessionId = runtime?.getStatus().sessionId;
    if (!sessionId) return;
    const existing = (await this.store.readSession<ProviderSessionState>(id)) ?? {};
    if (existing.sessionId === sessionId) return;
    await this.store.saveSession(id, { ...existing, sessionId });
  }

  private async closeMessageLoop(id: string): Promise<void> {
    const timer = this.pollTimers.get(id);
    if (timer) clearInterval(timer);
    this.pollTimers.delete(id);
    this.messageLoops.delete(id);
    this.ticking.delete(id);
    const handle = this.coreHandles.get(id);
    this.coreHandles.delete(id);
    if (handle && this.core) {
      await this.core.closeSession(handle).catch((error) => {
        void this.logs.append(id, `failed to close core session: ${errorMessage(error)}`);
      });
    }
  }
}

function slugifyAgentName(name: string): string {
  const maxSlugLength = 63;
  const slug = name
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
  if (!slug) return `agent-${crypto.randomUUID().slice(0, 8)}`;
  if (slug.length <= maxSlugLength) return slug;
  const suffix = crypto.randomUUID().replace(/-/g, "").slice(0, 8);
  const prefix = slug.slice(0, maxSlugLength - suffix.length - 1).replace(/-+$/g, "");
  return `${prefix || "agent"}-${suffix}`;
}

function applyPolicyUpdate(agent: AgentConfig, input: AgentPolicyUpdateInput): AgentConfig {
  const next: AgentConfig = {
    ...agent,
    ...(agent.deniedTools ? { deniedTools: [...agent.deniedTools] } : {}),
  };
  if (input.accessMode !== undefined) next.accessMode = parseAccessMode(input.accessMode);
  if (input.networkAccess !== undefined) next.networkAccess = parseNetworkAccess(input.networkAccess);
  if (input.deniedTools !== undefined) next.deniedTools = normalizeDeniedTools(input.deniedTools);
  if (input.fileAccess !== undefined) next.fileAccess = mergeFileAccess(next.fileAccess, input.fileAccess);
  if (input.providerConfigPaths !== undefined) {
    next.providerConfigPaths = normalizeProviderConfigPathsForProvider(
      next.provider,
      mergeProviderConfigPaths(next.providerConfigPaths, input.providerConfigPaths),
    );
  }
  if (input.projectPath === null) delete next.projectPath;
  else if (input.projectPath !== undefined) next.projectPath = input.projectPath;
  return next;
}

function draftAgentConfig(input: AgentCreateInput, stateRoot: string): AgentConfig {
  const id = crypto.randomUUID();
  const now = new Date().toISOString();
  const accessMode = parseAccessMode(input.accessMode);
  return {
    id,
    name: input.name,
    provider: input.provider,
    accessMode,
    networkAccess: parseNetworkAccess(
      input.networkAccess,
      accessMode === "trusted" ? "inherit" : defaultNetworkAccess(),
    ),
    deniedTools: normalizeDeniedTools(input.deniedTools),
    fileAccess: normalizeFileAccess(input.fileAccess),
    providerConfigPaths: normalizeProviderConfigPathsForProvider(input.provider, input.providerConfigPaths),
    instructions: input.instructions ?? "",
    ...(input.projectPath ? { projectPath: input.projectPath } : {}),
    ...(input.coreIdentity ? { coreIdentity: input.coreIdentity } : {}),
    workspace: agentWorkspace(stateRoot, id),
    status: "created",
    createdAt: now,
    updatedAt: now,
  };
}

function agentPolicySnapshot(agent: AgentConfig, policy: ResolvedPolicy): AgentPolicySnapshot {
  return {
    agent,
    policy: {
      accessMode: policy.accessMode,
      cwd: policy.cwd,
      agentHome: policy.agentHome,
      workspace: policy.workspace,
      ...(policy.projectPath ? { projectPath: policy.projectPath } : {}),
      networkAccess: policy.networkAccess,
      deniedTools: [...policy.deniedTools],
      fileAccess: {
        readablePaths: [...policy.fileAccess.readablePaths],
        writablePaths: [...policy.fileAccess.writablePaths],
      },
      providerConfigPaths: [...agent.providerConfigPaths],
      ...(policy.sandbox
        ? {
            sandbox: {
              ...policy.sandbox,
              readableRoots: [...policy.sandbox.readableRoots],
              writableRoots: [...policy.sandbox.writableRoots],
              deniedExecutables: [...policy.sandbox.deniedExecutables],
            },
          }
        : {}),
    },
  };
}

function errorMessage(error: unknown): string {
  return redact(error instanceof Error ? error.message : String(error));
}

function requireDeclaredOperatorPublicKey(
  value: unknown,
  message = "native core did not return declared operator public key for agent identity",
): string {
  const normalized = normalizeDeclaredOperatorPublicKey(value);
  if (!normalized) {
    throw new Error(message);
  }
  return normalized;
}

function normalizeAgentCreateInput(input: AgentCreateInput): AgentCreateInput {
  if (!input.coreIdentity) return input;
  const coreIdentity = normalizeSuppliedCoreIdentity(input.coreIdentity);
  return {
    ...input,
    coreIdentity,
  };
}

function normalizeSuppliedCoreIdentity(identity: CoreAgentIdentity): CoreAgentIdentity {
  assertCoreIdentityShape(identity, "supplied coreIdentity is invalid");
  return {
    operatorSlug: identity.operatorSlug,
    agentSlug: identity.agentSlug,
    identityType: "agent",
    declaredOperatorPublicKey: requireDeclaredOperatorPublicKey(
      identity.declaredOperatorPublicKey,
      "supplied coreIdentity does not include declared operator public key",
    ),
    source: "web_signed",
  };
}

function assertCoreIdentityShape(
  identity: Pick<CoreAgentIdentity, "operatorSlug" | "agentSlug" | "identityType">,
  message: string,
): void {
  if (!identity || !isValidCoreSlug(identity.operatorSlug) || !isValidCoreSlug(identity.agentSlug)) {
    throw new Error(`${message}: operatorSlug and agentSlug must be lowercase slugs`);
  }
  if (identity.identityType !== "agent") {
    throw new Error(`${message}: identityType must be agent`);
  }
}

function redactProviderStatus(status: ProviderStatus): ProviderStatus {
  if (!status.lastError) return status;
  return { ...status, lastError: redact(status.lastError) };
}
