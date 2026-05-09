import { extractTextFromEvents, parseJsonLines } from "../process/stream-json.js";

export function normalizeCodexOutput(stdout: string): string {
  const events = parseJsonLines(stdout);
  return extractTextFromEvents(events) || stdout.trim();
}

export function extractCodexSessionId(stdout: string): string | undefined {
  const events = parseJsonLines(stdout);
  for (const event of events) {
    if (!event || typeof event !== "object") continue;
    const sessionId = sessionIdFromEvent(event as Record<string, unknown>);
    if (sessionId) return sessionId;
  }
  return undefined;
}

function sessionIdFromEvent(event: Record<string, unknown>): string | undefined {
  const type = typeof event.type === "string" ? event.type.toLowerCase() : "";
  if (type.includes("session")) {
    const direct = firstString(event.id, event.session_id, event.sessionId, event.conversation_id, event.conversationId);
    if (direct) return direct;
  }
  const session = event.session;
  if (session && typeof session === "object") {
    const record = session as Record<string, unknown>;
    const id = firstString(record.id, record.session_id, record.sessionId);
    if (id) return id;
  }
  const conversation = event.conversation;
  if (conversation && typeof conversation === "object") {
    const record = conversation as Record<string, unknown>;
    const id = firstString(record.id, record.conversation_id, record.conversationId);
    if (id) return id;
  }
  return firstString(event.session_id, event.sessionId, event.conversation_id, event.conversationId);
}

function firstString(...values: unknown[]): string | undefined {
  return values.find((value): value is string => typeof value === "string" && value.trim().length > 0)?.trim();
}
