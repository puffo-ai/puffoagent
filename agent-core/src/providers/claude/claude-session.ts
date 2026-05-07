import { ResolvedPolicy } from "../../policy/policy.js";
import { AgentInput, AgentOutput, ProviderStatus } from "../../types.js";
import { CliProcess } from "../process/child-process.js";
import { ProviderSession } from "../provider-types.js";
import { buildClaudePrompt, claudePrintArgs } from "./claude-command.js";
import { classifyClaudeError } from "./claude-errors.js";
import { normalizeClaudeText } from "./claude-stream.js";

export class ClaudeSession implements ProviderSession {
  private status: ProviderStatus = { state: "idle" };

  constructor(
    private readonly policy: ResolvedPolicy,
    private readonly process = new CliProcess(),
    private sessionId?: string,
    private readonly command = "claude",
  ) {}

  async start(): Promise<void> {
    this.status = { state: "ready", ...(this.sessionId ? { sessionId: this.sessionId } : {}) };
  }

  async stop(): Promise<void> {
    this.status = { state: "stopped", ...(this.sessionId ? { sessionId: this.sessionId } : {}) };
  }

  async resetSession(): Promise<void> {
    this.sessionId = undefined;
    this.status = { state: "ready" };
  }

  getStatus(): ProviderStatus {
    return this.status;
  }

  async send(input: AgentInput): Promise<AgentOutput> {
    this.status = { state: "busy", ...(this.sessionId ? { sessionId: this.sessionId } : {}) };
    const prompt = buildClaudePrompt(input);
    const result = await this.process.runText(this.command, claudePrintArgs(prompt, this.sessionId), this.policy);
    if (result.code !== 0) {
      const error = classifyClaudeError(result.stderr);
      this.status = { state: "error", lastError: error, ...(this.sessionId ? { sessionId: this.sessionId } : {}) };
      return { kind: "error", error };
    }
    const text = normalizeClaudeText(result.stdout);
    this.status = { state: "ready", ...(this.sessionId ? { sessionId: this.sessionId } : {}) };
    if (!input.mustRespond && text === "[SILENT]") return { kind: "silent" };
    return { kind: "reply", body: text };
  }
}
