import { CommandRunner, findExecutable } from "../platform/shell.js";
import { sandboxExecutablePath } from "../policy/sandbox.js";
import { SandboxCapability } from "../types.js";

export async function detectSandbox(runner: CommandRunner): Promise<SandboxCapability> {
  if (process.platform !== "darwin") {
    return { supported: false, reason: "not_macos" };
  }
  const sandboxExec = sandboxExecutablePath() ?? await findExecutable("sandbox-exec", runner);
  if (!sandboxExec) return { supported: false, reason: "not_found" };
  return {
    supported: true,
    provider: "sandbox-exec",
    path: sandboxExec,
  };
}
