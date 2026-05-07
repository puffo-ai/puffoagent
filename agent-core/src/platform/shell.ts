import { execFile } from "node:child_process";
import { constants } from "node:fs";
import fs from "node:fs/promises";
import path from "node:path";

export interface CommandResult {
  code: number | null;
  stdout: string;
  stderr: string;
}

export interface CommandRunner {
  run(
    command: string,
    args?: string[],
    options?: {
      cwd?: string;
      env?: NodeJS.ProcessEnv;
      timeoutMs?: number;
      input?: string;
    },
  ): Promise<CommandResult>;
}

export class DefaultCommandRunner implements CommandRunner {
  run(
    command: string,
    args: string[] = [],
    options: {
      cwd?: string;
      env?: NodeJS.ProcessEnv;
      timeoutMs?: number;
      input?: string;
    } = {},
  ): Promise<CommandResult> {
    return new Promise((resolve) => {
      const child = execFile(
        command,
        args,
        {
          cwd: options.cwd,
          env: options.env,
          timeout: options.timeoutMs ?? 5000,
          windowsHide: true,
          maxBuffer: 1024 * 1024,
        },
        (error, stdout, stderr) => {
          const code =
            error && "code" in error && typeof error.code === "number"
              ? error.code
              : error
                ? 1
                : 0;
          resolve({
            code,
            stdout: String(stdout ?? ""),
            stderr: String(stderr || (error instanceof Error ? error.message : "")),
          });
        },
      );
      if (options.input !== undefined) {
        child.stdin?.end(options.input);
      }
    });
  }
}

export async function findExecutable(
  name: string,
  runner: CommandRunner = new DefaultCommandRunner(),
): Promise<string | undefined> {
  const locator = process.platform === "win32" ? "where" : "which";
  const result = await runner.run(locator, [name], { timeoutMs: 2500 });
  if (result.code !== 0) return undefined;
  return result.stdout
    .split(/\r?\n/)
    .map((line) => line.trim())
    .find(Boolean);
}

export async function resolveExecutablePath(
  name: string,
  pathValue = process.env.PATH,
): Promise<string | undefined> {
  const candidates = path.isAbsolute(name)
    ? executableCandidates(name)
    : pathEntries(pathValue).flatMap((entry) =>
        path.isAbsolute(entry) ? executableCandidates(path.join(entry, name)) : [],
      );

  for (const candidate of candidates) {
    if (await isExecutableFile(candidate)) return candidate;
  }
  return undefined;
}

function pathEntries(value: string | undefined): string[] {
  return (value || "")
    .split(path.delimiter)
    .map((entry) => entry.trim())
    .filter(Boolean);
}

function executableCandidates(base: string): string[] {
  if (process.platform !== "win32") return [base];
  const extensions = (process.env.PATHEXT || ".EXE;.CMD;.BAT;.COM")
    .split(";")
    .map((entry) => entry.trim())
    .filter(Boolean);
  if (extensions.some((extension) => base.toLowerCase().endsWith(extension.toLowerCase()))) {
    return [base];
  }
  return [base, ...extensions.map((extension) => `${base}${extension.toLowerCase()}`)];
}

async function isExecutableFile(file: string): Promise<boolean> {
  try {
    const stat = await fs.stat(file);
    if (!stat.isFile()) return false;
    await fs.access(file, constants.X_OK);
    return true;
  } catch {
    return false;
  }
}
