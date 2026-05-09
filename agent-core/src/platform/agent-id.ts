const SAFE_AGENT_ID = /^[A-Za-z0-9_-]{1,128}$/;

export function isSafeAgentId(agentId: unknown): agentId is string {
  return typeof agentId === "string" && SAFE_AGENT_ID.test(agentId);
}

export function assertSafeAgentId(agentId: string): void {
  if (!isSafeAgentId(agentId)) {
    throw new Error(`invalid agent id: ${agentId}`);
  }
}
