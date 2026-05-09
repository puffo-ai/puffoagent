import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { ResolvedPolicy, SandboxPolicy } from "./policy.js";

export interface LaunchCommand {
  command: string;
  args: string[];
}

const SYSTEM_READ_ROOTS = [
  "/bin",
  "/sbin",
  "/usr",
  "/System",
  "/Library",
  "/opt/homebrew",
  "/dev/null",
  "/dev/random",
  "/dev/urandom",
];

const DEFAULT_DENIED_TOOLS = [
  "security",
  "lsof",
  "ps",
  "dtrace",
  "fs_usage",
  "netstat",
  "scutil",
  "dscacheutil",
  "ioreg",
  "launchctl",
  "osascript",
  "lldb",
];

export function sandboxPolicyForResolvedPolicy(policy: Omit<ResolvedPolicy, "sandbox">): SandboxPolicy | undefined {
  if (process.platform !== "darwin" || policy.accessMode === "trusted" || !shouldApplySandbox(policy)) {
    return undefined;
  }
  const readableRoots = uniqueRoots([
    ...SYSTEM_READ_ROOTS,
    ...sandboxReadablePathEntries(policy.env.PATH),
    policy.agentHome,
    policy.workspace,
    ...policy.fileAccess.readablePaths,
    ...policy.fileAccess.writablePaths,
    ...(policy.projectPath ? [policy.projectPath] : []),
  ]);
  const writableRoots = uniqueRoots([
    policy.agentHome,
    policy.workspace,
    ...policy.fileAccess.writablePaths,
    ...(policy.accessMode === "project" && policy.projectPath ? [policy.projectPath] : []),
    process.env.TMPDIR || "/tmp",
  ]);
  return {
    enabled: true,
    platform: "darwin",
    network: policy.networkAccess,
    readableRoots,
    writableRoots,
    deniedExecutables: deniedExecutablePaths(policy.env.PATH, policy.deniedTools),
  };
}

export function applySandboxLaunch(
  command: string,
  args: string[],
  policy: ResolvedPolicy,
): LaunchCommand {
  if (!policy.sandbox?.enabled) return { command, args };
  return {
    command: sandboxExecutablePath() ?? "sandbox-exec",
    args: ["-p", buildDarwinSandboxProfile(policy.sandbox), command, ...args],
  };
}

export type SandboxExecutableProbe = (candidate: string) => boolean;

export function assertSandboxAvailable(
  policy: ResolvedPolicy,
  probe: SandboxExecutableProbe = executableProbe,
): void {
  if (!policy.sandbox?.enabled) return;
  if (sandboxExecutablePath(probe)) return;
  throw new Error("sandbox_unavailable: sandbox-exec is required for safe/project agents on macOS");
}

export function sandboxExecutablePath(probe: SandboxExecutableProbe = executableProbe): string | undefined {
  if (process.platform !== "darwin") return undefined;
  const candidates = uniqueRoots([
    "/usr/bin/sandbox-exec",
    ...pathEntries(process.env.PATH).map((entry) => path.join(entry, "sandbox-exec")),
  ]);
  return candidates.find(probe);
}

export function buildDarwinSandboxProfile(policy: SandboxPolicy): string {
  const readableRules = pathRules(policy.readableRoots);
  const writableRules = pathRules(policy.writableRoots);
  const deniedExecutableRules = literalPathRules(policy.deniedExecutables);
  return [
    "(version 1)",
    "(deny default)",
    "(allow process*)",
    deniedExecutableRules ? `(deny process-exec ${deniedExecutableRules})` : "",
    "(allow signal (target self))",
    "(allow sysctl-read)",
    "(allow mach-lookup)",
    "(allow file-read-metadata)",
    readableRules ? `(allow file-read* ${readableRules})` : "",
    writableRules ? `(allow file-write* ${writableRules})` : "",
    policy.network === "inherit" ? "(allow network*)" : "",
  ]
    .filter(Boolean)
    .join("\n");
}

function sandboxRequested(): boolean {
  return process.env.AGENT_CORE_SANDBOX === "1" || process.env.AGENT_CORE_SANDBOX === "darwin";
}

function shouldApplySandbox(policy: Omit<ResolvedPolicy, "sandbox">): boolean {
  if (sandboxRequested()) return true;
  if (sandboxDisabled() && policy.networkAccess !== "deny" && policy.deniedTools.length === 0) {
    return false;
  }
  return (
    policy.accessMode === "safe" ||
    policy.accessMode === "project" ||
    policy.networkAccess === "deny" ||
    policy.deniedTools.length > 0
  );
}

function sandboxDisabled(): boolean {
  const value = process.env.AGENT_CORE_SANDBOX?.toLowerCase();
  return value === "0" || value === "false" || value === "off";
}

function pathEntries(value: string | undefined): string[] {
  return (value || "")
    .split(path.delimiter)
    .map((entry) => entry.trim())
    .filter(Boolean);
}

function sandboxReadablePathEntries(value: string | undefined): string[] {
  return pathEntries(value).filter((entry) => {
    if (!path.isAbsolute(entry)) return false;
    const resolved = path.resolve(entry);
    return !isBroadReadablePathRoot(resolved);
  });
}

function isBroadReadablePathRoot(value: string): boolean {
  const home = path.resolve(os.homedir());
  return value === path.parse(value).root || value === home || value === path.dirname(home);
}

function executableProbe(candidate: string): boolean {
  try {
    fs.accessSync(candidate, fs.constants.X_OK);
    return true;
  } catch {
    return false;
  }
}

function deniedExecutablePaths(pathValue: string | undefined, deniedTools: string[] = []): string[] {
  const pathDirs = pathEntries(pathValue);
  const wellKnownDirs = ["/bin", "/sbin", "/usr/bin", "/usr/sbin"];
  return uniqueRoots([
    ...DEFAULT_DENIED_TOOLS.flatMap((tool) =>
      [...wellKnownDirs, ...pathDirs].map((dir) => path.join(dir, tool)),
    ),
    ...deniedTools.flatMap((tool) =>
      path.isAbsolute(tool) ? [tool] : [...wellKnownDirs, ...pathDirs].map((dir) => path.join(dir, tool)),
    ),
    ...extraDeniedExecutables(),
  ]);
}

function extraDeniedExecutables(): string[] {
  return (process.env.AGENT_CORE_DENIED_EXECUTABLES || "")
    .split(path.delimiter)
    .map((entry) => entry.trim())
    .filter(Boolean);
}

function uniqueRoots(roots: string[]): string[] {
  return Array.from(new Set(roots.map((root) => path.resolve(root))));
}

function pathRules(roots: string[]): string {
  return roots
    .map((root) => (isDeviceLiteral(root) ? `(literal ${schemeString(root)})` : `(subpath ${schemeString(root)})`))
    .join(" ");
}

function literalPathRules(roots: string[]): string {
  return roots.map((root) => `(literal ${schemeString(root)})`).join(" ");
}

function isDeviceLiteral(root: string): boolean {
  return root === "/dev/null" || root === "/dev/random" || root === "/dev/urandom";
}

function schemeString(value: string): string {
  return JSON.stringify(value);
}
