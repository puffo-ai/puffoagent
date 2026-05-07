export function parseJsonLines(text: string): unknown[] {
  return text
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => {
      try {
        return JSON.parse(line) as unknown;
      } catch {
        return { type: "text", text: line };
      }
    });
}

export function extractTextFromEvents(events: unknown[]): string {
  const chunks: string[] = [];
  for (const event of events) {
    appendEventText(chunks, event);
  }
  return uniqueAdjacent(chunks.map((chunk) => chunk.trim()).filter(Boolean)).join("\n").trim();
}

function appendEventText(chunks: string[], event: unknown): void {
  if (!event || typeof event !== "object") return;
  const value = event as Record<string, unknown>;

  for (const key of ["result", "text", "message", "content", "output"]) {
    appendTextValue(chunks, value[key]);
  }

  if (Array.isArray(value.items)) appendTextValue(chunks, value.items);
  if (Array.isArray(value.content)) appendTextValue(chunks, value.content);
  if (value.item && typeof value.item === "object") appendTextValue(chunks, value.item);
  if (value.delta && typeof value.delta === "object") appendTextValue(chunks, value.delta);
}

function appendTextValue(chunks: string[], value: unknown): void {
  if (typeof value === "string") {
    chunks.push(value);
    return;
  }
  if (Array.isArray(value)) {
    for (const item of value) appendTextValue(chunks, item);
    return;
  }
  if (!value || typeof value !== "object") return;
  const record = value as Record<string, unknown>;
  if (record.type === "text" || record.type === "output_text" || record.type === "message") {
    appendTextValue(chunks, record.text ?? record.content ?? record.message);
  }
  if (Array.isArray(record.content)) appendTextValue(chunks, record.content);
}

function uniqueAdjacent(values: string[]): string[] {
  const result: string[] = [];
  for (const value of values) {
    if (result.at(-1) !== value) result.push(value);
  }
  return result;
}
