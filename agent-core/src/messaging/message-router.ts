import { OpenedAgentMessage } from "../native/core.js";
import { AgentConfig } from "../types.js";
import { shouldDeliverToAgent } from "./message-filter.js";

export function routeMessages(agent: AgentConfig, messages: OpenedAgentMessage[]): OpenedAgentMessage[] {
  return messages.filter((message) => shouldDeliverToAgent(message, agent));
}
