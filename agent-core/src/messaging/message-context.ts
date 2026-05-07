import { AgentInput } from "../types.js";
import { OpenedAgentMessage } from "../native/core.js";

export function toAgentInput(message: OpenedAgentMessage, instructions: string): AgentInput {
  return {
    body: message.body,
    instructions,
    mustRespond: message.mustRespond,
    mentioned: message.mentioned,
    dm: message.dm,
    ...(message.senderSlug ? { senderSlug: message.senderSlug } : {}),
    ...(message.spaceId ? { spaceId: message.spaceId } : {}),
    ...(message.channelId ? { channelId: message.channelId } : {}),
    ...(message.threadRootId ? { threadRootId: message.threadRootId } : {}),
    ...(message.replyToId ? { replyToId: message.replyToId } : {}),
  };
}
