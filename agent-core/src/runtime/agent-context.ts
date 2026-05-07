import { AgentConfig } from "../types.js";
import { ProviderSession } from "../providers/provider-types.js";

export interface AgentRuntimeContext {
  config: AgentConfig;
  providerSession: ProviderSession;
}
