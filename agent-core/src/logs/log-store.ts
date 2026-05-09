import { constants } from "node:fs";
import fs from "node:fs/promises";
import path from "node:path";
import { assertSafeAgentId } from "../platform/agent-id.js";
import { assertDirectoryWithinRoot, ensureDirectory, isNotFound, unsafePathError } from "../platform/safe-directory.js";
import { redact } from "./redact.js";

const DEFAULT_TAIL_LINES = 200;
const MAX_TAIL_LINES = 1000;
const MAX_TAIL_BYTES = 1024 * 1024;

export class LogStore {
  constructor(private readonly root: string) {}

  async append(agentId: string, line: string): Promise<void> {
    assertSafeAgentId(agentId);
    const logDir = path.join(this.root, "agents", agentId, "logs");
    await ensureDirectory(logDir, { root: this.root });
    const entry = `${new Date().toISOString()} ${redact(line)}\n`;
    const file = path.join(logDir, "agent.log");
    const handle = await fs.open(
      file,
      constants.O_APPEND | constants.O_CREAT | constants.O_WRONLY | noFollowFlag(),
      0o600,
    );
    try {
      await handle.writeFile(entry, "utf8");
    } finally {
      await handle.close();
    }
    await fs.chmod(file, 0o600).catch(() => undefined);
  }

  async tail(agentId: string, maxLines = DEFAULT_TAIL_LINES): Promise<string[]> {
    assertSafeAgentId(agentId);
    const lineLimit = normalizeTailLines(maxLines);
    if (lineLimit === 0) return [];

    const file = path.join(this.root, "agents", agentId, "logs", "agent.log");
    let handle: fs.FileHandle | undefined;
    try {
      await assertDirectoryWithinRoot(path.dirname(file), this.root);
      handle = await fs.open(file, constants.O_RDONLY | noFollowFlag());
      const stat = await handle.stat();
      if (!stat.isFile()) throw unsafePathError(file);

      const start = Math.max(0, stat.size - MAX_TAIL_BYTES);
      const length = stat.size - start;
      const buffer = Buffer.alloc(length);
      if (length > 0) {
        await handle.read(buffer, 0, length, start);
      }

      const text = buffer.toString("utf8").trimEnd();
      if (!text) return [];
      const lines = text.split(/\r?\n/);
      if (start > 0) lines.shift();
      return lines.slice(-lineLimit).map((line) => redact(line));
    } catch (error) {
      if (isNotFound(error)) return [];
      throw error;
    } finally {
      await handle?.close().catch(() => undefined);
    }
  }
}

function normalizeTailLines(maxLines: number): number {
  if (!Number.isFinite(maxLines)) return DEFAULT_TAIL_LINES;
  return Math.max(0, Math.min(MAX_TAIL_LINES, Math.floor(maxLines)));
}

function noFollowFlag(): number {
  return "O_NOFOLLOW" in constants ? constants.O_NOFOLLOW : 0;
}
