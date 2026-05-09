import { AgentInput, AgentOutput, ProviderStatus } from "../types.js";

export interface ProviderSession {
  start(): Promise<void>;
  stop(): Promise<void>;
  send(input: AgentInput): Promise<AgentOutput>;
  resetSession(): Promise<void>;
  getStatus(): ProviderStatus;
}
