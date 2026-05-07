import { AgentInput, AgentOutput, ProviderStatus } from "../types.js";
import { AgentRuntimeContext } from "./agent-context.js";

export class AgentRuntime {
  constructor(private readonly context: AgentRuntimeContext) {}

  async start(): Promise<void> {
    await this.context.providerSession.start();
  }

  async stop(): Promise<void> {
    await this.context.providerSession.stop();
  }

  async resetSession(): Promise<void> {
    await this.context.providerSession.resetSession();
  }

  async handle(input: AgentInput): Promise<AgentOutput> {
    return this.context.providerSession.send(input);
  }

  getStatus(): ProviderStatus {
    return this.context.providerSession.getStatus();
  }
}
