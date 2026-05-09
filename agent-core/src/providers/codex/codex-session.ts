import { ResolvedPolicy } from "../../policy/policy.js";
import { AgentInput, AgentOutput, ProviderStatus } from "../../types.js";
import { CliProcess } from "../process/child-process.js";
import { ProviderSession } from "../provider-types.js";
import { buildCodexPrompt, codexExecArgs } from "./codex-command.js";
import { classifyCodexError } from "./codex-errors.js";
import { extractCodexSessionId, normalizeCodexOutput } from "./codex-stream.js";

export class CodexSession implements ProviderSession {
  private status: ProviderStatus = { state: "idle" };

  constructor(
    private readonly policy: ResolvedPolicy,
    private readonly process = new CliProcess(),
    private sessionId?: string,
    private readonly command = "codex",
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
    const prompt = buildCodexPrompt(input);
    const result = await this.process.runText(this.command, codexExecArgs(prompt, this.policy.cwd, this.sessionId), this.policy);
    if (result.code !== 0) {
      const error = classifyCodexError(result.stderr);
      this.status = { state: "error", lastError: error, ...(this.sessionId ? { sessionId: this.sessionId } : {}) };
      return { kind: "error", error };
    }
    this.sessionId = extractCodexSessionId(result.stdout) ?? this.sessionId;
    const text = normalizeCodexOutput(result.stdout);
    this.status = { state: "ready", ...(this.sessionId ? { sessionId: this.sessionId } : {}) };
    if (!input.mustRespond && text === "[SILENT]") return { kind: "silent" };
    return { kind: "reply", body: text };
  }
}
