import { ResolvedPolicy } from "../policy/policy.js";
import { ProviderKind } from "../types.js";
import { ClaudeSession } from "./claude/claude-session.js";
import { CodexSession } from "./codex/codex-session.js";
import { ProviderSession } from "./provider-types.js";

export interface ProviderSessionState {
  sessionId?: string;
  commandPath?: string;
}

export function createProviderSession(
  provider: ProviderKind,
  policy: ResolvedPolicy,
  state: ProviderSessionState = {},
): ProviderSession {
  if (provider === "claude") return new ClaudeSession(policy, undefined, state.sessionId, state.commandPath);
  return new CodexSession(policy, undefined, state.sessionId, state.commandPath);
}
