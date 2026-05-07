import { AgentOutput } from "../types.js";
import { redact } from "../logs/redact.js";

export function normalizeReply(output: AgentOutput, mustRespond = false): string | undefined {
  if (output.kind === "reply") {
    const body = output.body ?? "";
    return body.trim() || !mustRespond ? body : "I received this, but did not produce a response.";
  }
  if (output.kind === "need_more_info") return output.body || "I need more information to continue.";
  if (output.kind === "permission_denied") return output.body || "I do not have permission to do that.";
  if (output.kind === "error") return output.error ? `Agent error: ${redact(output.error)}` : "Agent error.";
  if (mustRespond) return "I received this, but did not produce a response.";
  return undefined;
}
