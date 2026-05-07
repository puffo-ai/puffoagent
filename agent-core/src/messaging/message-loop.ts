import { AgentRuntime } from "../runtime/agent-runtime.js";
import { AgentConfig } from "../types.js";
import { CoreNative, CoreSessionHandle, OpenedAgentMessage } from "../native/core.js";
import { normalizeReply } from "./delivery.js";
import { routeMessages } from "./message-router.js";
import { toAgentInput } from "./message-context.js";

export class MessageLoop {
  constructor(
    private readonly core: CoreNative,
    private readonly handle: CoreSessionHandle,
    private readonly agent: AgentConfig,
    private readonly runtime: AgentRuntime,
  ) {}

  async tick(): Promise<number> {
    const pending = await this.core.processPendingMessages(this.handle);
    const messages = routeMessages(this.agent, pending);
    let handled = 0;
    for (const message of messages) {
      const output = await this.handleMessage(message);
      const body = normalizeReply(output, message.mustRespond);
      if (body) {
        if (message.channelId && message.spaceId) {
          await this.core.sendChannelReply(this.handle, {
            spaceId: message.spaceId,
            channelId: message.channelId,
            body,
            threadRootId: message.threadRootId,
            replyToId: message.id,
          });
        } else if (message.senderSlug) {
          await this.core.sendDirectReply(this.handle, {
            recipientSlug: message.senderSlug,
            body,
            replyToId: message.id,
          });
        }
      }
      handled += 1;
    }
    return handled;
  }

  private async handleMessage(message: OpenedAgentMessage) {
    try {
      return await this.runtime.handle(toAgentInput(message, this.agent.instructions));
    } catch (error) {
      return {
        kind: "error" as const,
        error: error instanceof Error ? error.message : String(error),
      };
    }
  }
}
