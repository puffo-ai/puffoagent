import { AgentInput } from "../../types.js";

export function buildCodexPrompt(input: AgentInput): string {
  const parts = [
    input.instructions ? `Instructions:\n${input.instructions}` : undefined,
    `Context:\n${contextLines(input).join("\n")}`,
    `Message:\n${input.body}`,
    `mustRespond: ${input.mustRespond}`,
    "If no response is needed and mustRespond is false, output exactly: [SILENT]",
  ].filter(Boolean);
  return parts.join("\n\n");
}

export function codexExecArgs(prompt: string, cwd: string, sessionId?: string): string[] {
  if (sessionId) return ["exec", "resume", "--json", "--skip-git-repo-check", sessionId, prompt];
  return ["exec", "--json", "--cd", cwd, "--skip-git-repo-check", prompt];
}

function contextLines(input: AgentInput): string[] {
  return [
    `mentioned: ${input.mentioned}`,
    `dm: ${input.dm}`,
    input.senderSlug ? `sender: ${input.senderSlug}` : undefined,
    input.spaceId ? `space: ${input.spaceId}` : undefined,
    input.channelId ? `channel: ${input.channelId}` : undefined,
    input.threadRootId ? `thread: ${input.threadRootId}` : undefined,
    input.replyToId ? `replyTo: ${input.replyToId}` : undefined,
  ].filter((line): line is string => Boolean(line));
}
