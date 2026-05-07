import { RuntimeManager } from "../runtime/runtime-manager.js";

export interface ResumeReport {
  attempted: number;
  started: number;
  failed: Array<{ id: string; error: string }>;
}

export class DaemonSupervisor {
  constructor(readonly runtime: RuntimeManager) {}

  async startPersistedRunningAgents(): Promise<ResumeReport> {
    const agents = await this.runtime.listAgents();
    const report: ResumeReport = { attempted: 0, started: 0, failed: [] };
    for (const agent of agents.filter((agent) => agent.status === "running")) {
      report.attempted += 1;
      if (!agent.coreIdentity) {
        const error = "agent must have coreIdentity before daemon resume";
        await this.runtime.markAgentError(agent.id, error).catch(() => undefined);
        report.failed.push({ id: agent.id, error });
        continue;
      }
      try {
        await this.runtime.startAgent(agent.id);
        report.started += 1;
      } catch (error) {
        report.failed.push({
          id: agent.id,
          error: error instanceof Error ? error.message : String(error),
        });
      }
    }
    return report;
  }

  async stopAll(): Promise<void> {
    const agents = await this.runtime.listAgents();
    await Promise.all(
      agents
        .filter((agent) => agent.status === "running")
        .map((agent) => this.runtime.stopAgent(agent.id).catch(() => undefined)),
    );
  }
}
