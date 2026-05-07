import { AgentOutput } from "../types.js";
import { normalizeReply } from "./delivery.js";

export function requiresVisibleReply(output: AgentOutput, mustRespond: boolean): boolean {
  return Boolean(normalizeReply(output, mustRespond));
}
