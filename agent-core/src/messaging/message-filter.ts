import { OpenedAgentMessage } from "../native/core.js";
import { AgentConfig } from "../types.js";

export function shouldDeliverToAgent(message: OpenedAgentMessage, agent: AgentConfig): boolean {
  const selfSlugs = new Set<string>([agent.id]);
  if (agent.coreIdentity?.agentSlug) selfSlugs.add(agent.coreIdentity.agentSlug);
  if (message.senderSlug && selfSlugs.has(message.senderSlug)) return false;
  return true;
}
