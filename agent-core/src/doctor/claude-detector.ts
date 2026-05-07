import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { ProviderCheck } from "../types.js";
import { CommandRunner, findExecutable } from "../platform/shell.js";

export async function detectClaude(runner: CommandRunner): Promise<ProviderCheck> {
  const executable = await findExecutable("claude", runner);
  if (!executable) {
    return {
      provider: "claude",
      installed: false,
      ready: false,
      reason: "not_found",
      fixCommand: "Install Claude Code, then run claude login",
    };
  }

  const version = await readVersion(executable, runner);
  if (version.failed) {
    return {
      provider: "claude",
      installed: true,
      ready: false,
      path: executable,
      reason: "crashed",
      fixCommand: "Run claude --version; if it fails, reinstall Claude Code",
    };
  }
  const authStatus = await detectClaudeAuth();
  return {
    provider: "claude",
    installed: true,
    ready: true,
    path: executable,
    ...(version.value ? { version: version.value } : {}),
    authStatus,
    ...(authStatus === "unknown"
      ? { warnings: ["Claude auth is best-effort; run claude login if execution fails."] }
      : {}),
  };
}

async function readVersion(executable: string, runner: CommandRunner): Promise<{ failed: boolean; value?: string }> {
  const result = await runner.run(executable, ["--version"], { timeoutMs: 3000 });
  if (result.code !== 0) return { failed: true };
  const value = result.stdout.trim() || result.stderr.trim();
  return value ? { failed: false, value } : { failed: false };
}

async function detectClaudeAuth(): Promise<"ready" | "unknown"> {
  if (process.env.ANTHROPIC_API_KEY) return "ready";
  const home = os.homedir();
  const hints = [
    path.join(home, ".claude", ".credentials.json"),
    path.join(home, ".claude.json"),
  ];
  for (const hint of hints) {
    try {
      await fs.access(hint);
      return "ready";
    } catch {
      // Best-effort only. Claude Code may use Keychain with no file hint.
    }
  }
  return "unknown";
}
