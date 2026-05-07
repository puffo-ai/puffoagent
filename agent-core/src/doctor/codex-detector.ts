import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { ProviderCheck } from "../types.js";
import { CommandRunner, findExecutable } from "../platform/shell.js";

export async function detectCodex(runner: CommandRunner): Promise<ProviderCheck> {
  const executable = await findExecutable("codex", runner);
  if (!executable) {
    return {
      provider: "codex",
      installed: false,
      ready: false,
      reason: "not_found",
      fixCommand: "Install Codex CLI, then run codex login",
    };
  }

  const version = await readVersion(executable, runner);
  if (version.failed) {
    return {
      provider: "codex",
      installed: true,
      ready: false,
      path: executable,
      reason: "crashed",
      fixCommand: "Run codex --version; if it fails, reinstall Codex CLI",
    };
  }
  const authStatus = await detectCodexAuth();
  return {
    provider: "codex",
    installed: true,
    ready: authStatus !== "missing",
    path: executable,
    ...(version.value ? { version: version.value } : {}),
    authStatus,
    ...(authStatus === "missing"
      ? { reason: "not_logged_in", fixCommand: "codex login" }
      : {}),
  };
}

async function readVersion(executable: string, runner: CommandRunner): Promise<{ failed: boolean; value?: string }> {
  const result = await runner.run(executable, ["--version"], { timeoutMs: 3000 });
  if (result.code !== 0) return { failed: true };
  const value = result.stdout.trim() || result.stderr.trim();
  return value ? { failed: false, value } : { failed: false };
}

async function detectCodexAuth(): Promise<"ready" | "missing"> {
  if (process.env.OPENAI_API_KEY) return "ready";
  const authPath = path.join(os.homedir(), ".codex", "auth.json");
  try {
    await fs.access(authPath);
    return "ready";
  } catch {
    return "missing";
  }
}
