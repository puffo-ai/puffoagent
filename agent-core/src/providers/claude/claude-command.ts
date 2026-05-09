import { AgentInput } from "../../types.js";

export function buildClaudePrompt(input: AgentInput): string {
  const context = [
    input.instructions ? `Instructions:\n${input.instructions}` : undefined,
    `Context:\n${contextLines(input).join("\n")}`,
    `Message:\n${input.body}`,
    `mustRespond: ${input.mustRespond}`,
  ]
    .filter(Boolean)
    .join("\n\n");
  return `${context}\n\nReturn a concise response. If no reply is needed and mustRespond is false, say exactly: [SILENT]`;
}

export function claudePrintArgs(prompt: string, sessionId?: string): string[] {
  const args = [
    "--print",
    "--output-format",
    "text",
    "--permission-mode",
    "default",
  ];
  if (sessionId) args.push("--session-id", sessionId);
  args.push(prompt);
  return args;
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
