export type ProviderKind = "claude" | "codex";
export type AccessMode = "safe" | "project" | "trusted";
export type NetworkAccess = "inherit" | "deny";
export type AgentStatus = "created" | "running" | "stopped" | "error";

export interface FileAccessInput {
  readablePaths?: string[];
  writablePaths?: string[];
}

export interface FileAccessPolicy {
  readablePaths: string[];
  writablePaths: string[];
}

export interface AgentCreateInput {
  name: string;
  provider: ProviderKind;
  accessMode?: AccessMode;
  networkAccess?: NetworkAccess;
  deniedTools?: string[];
  fileAccess?: FileAccessInput;
  providerConfigPaths?: string[];
  instructions?: string;
  projectPath?: string;
  operatorSlug?: string;
  agentSlug?: string;
  coreIdentity?: CoreAgentIdentity;
  start?: boolean;
}

export interface AgentPolicyUpdateInput {
  accessMode?: AccessMode;
  networkAccess?: NetworkAccess;
  deniedTools?: string[];
  fileAccess?: FileAccessInput | null;
  providerConfigPaths?: string[] | null;
  projectPath?: string | null;
}

export interface CoreAgentIdentity {
  operatorSlug: string;
  agentSlug: string;
  identityType: "agent";
  declaredOperatorPublicKey: string;
  source?: "native" | "web_signed";
}

export interface AgentConfig {
  id: string;
  name: string;
  provider: ProviderKind;
  accessMode: AccessMode;
  networkAccess?: NetworkAccess;
  deniedTools?: string[];
  fileAccess: FileAccessPolicy;
  providerConfigPaths: string[];
  instructions: string;
  projectPath?: string;
  workspace: string;
  status: AgentStatus;
  createdAt: string;
  updatedAt: string;
  lastActiveAt?: string;
  lastError?: string;
  coreIdentity?: CoreAgentIdentity;
}

export interface ProviderCheck {
  provider: ProviderKind;
  installed: boolean;
  ready: boolean;
  path?: string;
  version?: string;
  authStatus?: "ready" | "missing" | "unknown";
  reason?: "not_found" | "not_logged_in" | "too_old" | "crashed" | "unknown";
  fixCommand?: string;
  warnings?: string[];
}

export interface EnvironmentReport {
  os: NodeJS.Platform;
  arch: NodeJS.Architecture;
  nodeVersion: string;
  server: ServerConnectivity;
  sandbox: SandboxCapability;
  providers: Record<ProviderKind, ProviderCheck>;
}

export interface ServerConnectivity {
  url: string;
  status: "reachable" | "unreachable" | "skipped";
  reachable: boolean;
  reason?: string;
}

export interface SandboxCapability {
  supported: boolean;
  provider?: "sandbox-exec";
  path?: string;
  reason?: "not_macos" | "not_found";
}

export interface AgentInput {
  body: string;
  instructions: string;
  mustRespond: boolean;
  mentioned: boolean;
  dm: boolean;
  senderSlug?: string;
  spaceId?: string;
  channelId?: string;
  threadRootId?: string;
  replyToId?: string;
  recentHistory?: Array<{ sender: string; body: string }>;
}

export type AgentOutputKind =
  | "reply"
  | "silent"
  | "need_more_info"
  | "permission_denied"
  | "error";

export interface AgentOutput {
  kind: AgentOutputKind;
  body?: string;
  error?: string;
  raw?: unknown;
}

export interface ProviderStatus {
  state: "idle" | "ready" | "busy" | "stopped" | "error";
  sessionId?: string;
  lastError?: string;
}
