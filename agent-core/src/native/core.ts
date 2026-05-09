export type CoreSessionHandle = string;

export interface DeviceStatus {
  connected: boolean;
  deviceId?: string;
  status: "ready" | "unavailable" | "pairing_required";
  reason?: string;
  blockedBy?: string[];
  nextAction?: string;
  missingConfig?: string[];
  serverUrl?: string;
  authTokenSource?: "env" | "keychain" | "memory";
}

export interface PairingStatus {
  status: "pending" | "confirmed" | "expired" | "canceled" | "unavailable";
  pairingId?: string;
  userCode?: string;
  confirmUrl?: string;
  expiresAt?: string;
  pollAfterMs?: number;
  operatorSlug?: string;
  accountId?: string;
  operatorBootstrap?: unknown;
  localWebGrant?: unknown;
  localGrant?: unknown;
  core?: DeviceStatus;
  reason?: string;
  blockedBy?: string[];
  nextAction?: string;
}

export interface OpenedAgentMessage {
  id: string;
  body: string;
  senderSlug?: string;
  spaceId?: string;
  channelId?: string;
  threadRootId?: string;
  replyToId?: string;
  mentioned: boolean;
  dm: boolean;
  mustRespond: boolean;
}

export interface MessageRef {
  messageId: string;
}

export interface DevInjectedChannelMessage {
  messageId: string;
  spaceId: string;
  channelId: string;
}

export interface CoreNative {
  openOrCreateDevice(input: Record<string, unknown>): Promise<DeviceStatus>;
  startPairing(input: Record<string, unknown>): Promise<PairingStatus>;
  confirmPairing(input: Record<string, unknown>): Promise<DeviceStatus>;
  createAgentIdentity?(input: {
    operatorSlug: string;
    agentSlug: string;
  }): Promise<{
    ok: boolean;
    operatorSlug: string;
    agentSlug: string;
    identityType: "agent";
    declaredOperatorPublicKey: string;
  }>;
  openAgentSession(input: Record<string, unknown>): Promise<CoreSessionHandle>;
  syncOnce(handle: CoreSessionHandle): Promise<unknown>;
  processPendingMessages(handle: CoreSessionHandle): Promise<OpenedAgentMessage[]>;
  sendChannelReply(handle: CoreSessionHandle, input: Record<string, unknown>): Promise<MessageRef>;
  sendDirectReply(handle: CoreSessionHandle, input: Record<string, unknown>): Promise<MessageRef>;
  snapshot(handle: CoreSessionHandle): Promise<unknown>;
  closeSession(handle: CoreSessionHandle): Promise<void>;
  devInjectChannelMessage?(input: {
    senderSlug: string;
    agentSlug: string;
    body: string;
  }): Promise<DevInjectedChannelMessage>;
  shutdown?(): Promise<void>;
}

export class NativeCoreUnavailableError extends Error {
  constructor(operation: string) {
    super(`core native binding is not available for ${operation}`);
    this.name = "NativeCoreUnavailableError";
  }
}

export class UnavailableCoreNative implements CoreNative {
  async openOrCreateDevice(): Promise<DeviceStatus> {
    return {
      connected: false,
      status: "unavailable",
      reason: "Rust core native binding has not been linked yet.",
      blockedBy: ["native_core_unavailable"],
      nextAction: "Install a package that includes the native sidecar or set AGENT_CORE_SIDECAR_BIN to an absolute sidecar path.",
    };
  }

  async startPairing(): Promise<PairingStatus> {
    return {
      status: "unavailable",
      reason: "Rust core native binding has not been linked yet.",
      blockedBy: ["native_core_unavailable"],
      nextAction: "Install a package that includes the native sidecar or set AGENT_CORE_SIDECAR_BIN to an absolute sidecar path.",
    };
  }

  async confirmPairing(): Promise<DeviceStatus> {
    return {
      connected: false,
      status: "unavailable",
      reason: "Rust core native binding has not been linked yet.",
      blockedBy: ["native_core_unavailable"],
      nextAction: "Install a package that includes the native sidecar or set AGENT_CORE_SIDECAR_BIN to an absolute sidecar path.",
    };
  }

  async openAgentSession(): Promise<CoreSessionHandle> {
    throw new NativeCoreUnavailableError("openAgentSession");
  }

  async syncOnce(): Promise<unknown> {
    throw new NativeCoreUnavailableError("syncOnce");
  }

  async processPendingMessages(): Promise<OpenedAgentMessage[]> {
    throw new NativeCoreUnavailableError("processPendingMessages");
  }

  async sendChannelReply(): Promise<MessageRef> {
    throw new NativeCoreUnavailableError("sendChannelReply");
  }

  async sendDirectReply(): Promise<MessageRef> {
    throw new NativeCoreUnavailableError("sendDirectReply");
  }

  async snapshot(): Promise<unknown> {
    throw new NativeCoreUnavailableError("snapshot");
  }

  async closeSession(): Promise<void> {
    throw new NativeCoreUnavailableError("closeSession");
  }
}
