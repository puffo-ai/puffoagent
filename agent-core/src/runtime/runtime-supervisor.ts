import { AgentRuntime } from "./agent-runtime.js";

export class RuntimeSupervisor {
  private readonly runtimes = new Map<string, AgentRuntime>();

  set(id: string, runtime: AgentRuntime): void {
    this.runtimes.set(id, runtime);
  }

  get(id: string): AgentRuntime | undefined {
    return this.runtimes.get(id);
  }

  delete(id: string): void {
    this.runtimes.delete(id);
  }
}
