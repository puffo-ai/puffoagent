import crypto from "node:crypto";
import { constants as fsConstants, type Stats } from "node:fs";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { ProviderKind } from "../types.js";

export interface ProviderCredentialProjectionOptions {
  sourceHome?: string;
  enabled?: boolean;
  extraPaths?: string[];
}

const PROVIDER_CREDENTIAL_FILES: Record<ProviderKind, string[]> = {
  codex: [
    ".codex/auth.json",
    ".codex/config.toml",
  ],
  claude: [
    ".claude.json",
    ".claude/.credentials.json",
    ".claude/settings.json",
  ],
};

const MAX_PROJECTED_FILE_BYTES = 2 * 1024 * 1024;
const MAX_PROJECTED_TOTAL_BYTES = 16 * 1024 * 1024;
const MAX_PROJECTED_FILES = 512;
const MAX_PROJECTED_DIRECTORY_DEPTH = 16;
const COPY_CHUNK_BYTES = 64 * 1024;
const READ_NOFOLLOW_FLAGS = fsConstants.O_RDONLY | (fsConstants.O_NOFOLLOW ?? 0);

interface ProjectionBudget {
  files: number;
  bytes: number;
}

export async function projectProviderCredentials(
  provider: ProviderKind,
  agentHome: string,
  options: ProviderCredentialProjectionOptions = {},
): Promise<string[]> {
  if (options.enabled === false || process.env.AGENT_CORE_CREDENTIALS === "off") return [];
  const sourceHome = options.sourceHome ?? os.homedir();
  const copied: string[] = [];
  const budget: ProjectionBudget = { files: 0, bytes: 0 };
  for (const relativePath of [...PROVIDER_CREDENTIAL_FILES[provider], ...(options.extraPaths ?? [])]) {
    const source = path.join(sourceHome, relativePath);
    const target = path.join(agentHome, relativePath);
    if (await copySafePath(sourceHome, source, target, agentHome, budget, 0)) copied.push(relativePath);
  }
  return copied;
}

async function copySafePath(
  sourceRoot: string,
  source: string,
  target: string,
  agentHome: string,
  budget: ProjectionBudget,
  depth: number,
): Promise<boolean> {
  const sourceStat = await safeSourceStat(sourceRoot, source);
  if (!sourceStat) return false;
  if (sourceStat.isFile()) return copyFileIntoAgentHome(source, target, agentHome, sourceStat, budget);
  if (!sourceStat.isDirectory()) return false;
  return copyDirectory(sourceRoot, source, target, agentHome, budget, depth);
}

async function copyFileIntoAgentHome(
  source: string,
  target: string,
  agentHome: string,
  sourceStat: Stats,
  budget: ProjectionBudget,
): Promise<boolean> {
  if (!canProjectFile(sourceStat.size, budget)) return false;
  const handle = await openRegularSourceFile(source);
  if (!handle) return false;
  let data: Buffer | undefined;
  try {
    const stat = await handle.stat();
    if (!isSameFile(sourceStat, stat)) return false;
    if (!canProjectFile(stat.size, budget)) return false;
    data = await readFileWithinLimit(handle, Math.min(MAX_PROJECTED_FILE_BYTES, MAX_PROJECTED_TOTAL_BYTES - budget.bytes));
  } finally {
    await handle.close().catch(() => undefined);
  }
  if (!data || !canProjectFile(data.byteLength, budget)) return false;

  const targetDir = path.dirname(target);
  if (!(await ensureSafeTargetDirectory(agentHome, targetDir))) return false;
  const tmp = path.join(targetDir, `.agent-core-${process.pid}-${crypto.randomUUID()}.tmp`);
  try {
    await fs.writeFile(tmp, data, { flag: "wx", mode: 0o600 });
    await fs.chmod(tmp, 0o600).catch(() => undefined);
    await fs.rename(tmp, target);
    await fs.chmod(target, 0o600).catch(() => undefined);
  } catch (error) {
    await fs.rm(tmp, { force: true }).catch(() => undefined);
    throw error;
  }
  budget.files += 1;
  budget.bytes += data.byteLength;
  return true;
}

async function copyDirectory(
  sourceRoot: string,
  source: string,
  target: string,
  agentHome: string,
  budget: ProjectionBudget,
  depth: number,
): Promise<boolean> {
  if (depth > MAX_PROJECTED_DIRECTORY_DEPTH) return false;
  const entries = await fs.readdir(source, { withFileTypes: true });
  let copiedAny = false;
  for (const entry of entries) {
    const childSource = path.join(source, entry.name);
    const childTarget = path.join(target, entry.name);
    const childStat = await safeSourceStat(sourceRoot, childSource);
    if (!childStat) continue;
    if (childStat.isDirectory()) {
      copiedAny = (await copyDirectory(sourceRoot, childSource, childTarget, agentHome, budget, depth + 1)) || copiedAny;
    } else if (entry.isFile()) {
      copiedAny = (await copyFileIntoAgentHome(childSource, childTarget, agentHome, childStat, budget)) || copiedAny;
    }
  }
  return copiedAny;
}

async function safeSourceStat(sourceRoot: string, source: string): Promise<Stats | undefined> {
  if (!(await sourcePathStaysWithinRoot(sourceRoot, source))) return undefined;
  try {
    const stat = await fs.lstat(source);
    if (stat.isSymbolicLink()) return undefined;
    return stat;
  } catch (error) {
    if (isNotFound(error)) return undefined;
    throw error;
  }
}

async function sourcePathStaysWithinRoot(sourceRoot: string, source: string): Promise<boolean> {
  const root = path.resolve(sourceRoot);
  let rootStat;
  try {
    rootStat = await fs.lstat(root);
  } catch (error) {
    if (isNotFound(error)) return false;
    throw error;
  }
  if (rootStat.isSymbolicLink() || !rootStat.isDirectory()) return false;

  const relative = path.relative(root, path.resolve(source));
  if (relative.startsWith("..") || path.isAbsolute(relative)) return false;
  let current = root;
  for (const segment of relative.split(path.sep).filter(Boolean)) {
    current = path.join(current, segment);
    let stat;
    try {
      stat = await fs.lstat(current);
    } catch (error) {
      if (isNotFound(error)) return false;
      throw error;
    }
    if (stat.isSymbolicLink()) return false;
  }
  return true;
}

function canProjectFile(sourceSize: number, budget: ProjectionBudget): boolean {
  if (!Number.isSafeInteger(sourceSize) || sourceSize < 0) return false;
  if (sourceSize > MAX_PROJECTED_FILE_BYTES) return false;
  if (budget.files >= MAX_PROJECTED_FILES) return false;
  if (budget.bytes + sourceSize > MAX_PROJECTED_TOTAL_BYTES) return false;
  return true;
}

function isSameFile(expected: Stats, actual: Stats): boolean {
  return expected.dev === actual.dev && expected.ino === actual.ino;
}

async function ensureSafeTargetDirectory(root: string, targetDir: string): Promise<boolean> {
  const relative = path.relative(root, targetDir);
  if (relative.startsWith("..") || path.isAbsolute(relative)) return false;
  let current = root;
  for (const segment of relative.split(path.sep).filter(Boolean)) {
    current = path.join(current, segment);
    try {
      const stat = await fs.lstat(current);
      if (!stat.isDirectory() || stat.isSymbolicLink()) return false;
      await fs.chmod(current, 0o700).catch(() => undefined);
    } catch (error) {
      if (!isNotFound(error)) throw error;
      await fs.mkdir(current, { mode: 0o700 });
    }
  }
  return true;
}

async function openRegularSourceFile(source: string): Promise<fs.FileHandle | undefined> {
  try {
    const handle = await fs.open(source, READ_NOFOLLOW_FLAGS);
    const stat = await handle.stat();
    if (!stat.isFile()) {
      await handle.close().catch(() => undefined);
      return undefined;
    }
    return handle;
  } catch (error) {
    if (isNotFound(error) || isUnsafeSourceOpenError(error)) return undefined;
    throw error;
  }
}

async function readFileWithinLimit(handle: fs.FileHandle, maxBytes: number): Promise<Buffer | undefined> {
  if (maxBytes < 0) return undefined;
  const chunks: Buffer[] = [];
  let total = 0;
  let position = 0;
  while (true) {
    const remaining = maxBytes + 1 - total;
    if (remaining <= 0) return undefined;
    const buffer = Buffer.alloc(Math.min(COPY_CHUNK_BYTES, remaining));
    const { bytesRead } = await handle.read(buffer, 0, buffer.length, position);
    if (bytesRead === 0) break;
    total += bytesRead;
    if (total > maxBytes) return undefined;
    chunks.push(Buffer.from(buffer.subarray(0, bytesRead)));
    position += bytesRead;
  }
  return Buffer.concat(chunks, total);
}

function isNotFound(error: unknown): boolean {
  return Boolean(error && typeof error === "object" && "code" in error && error.code === "ENOENT");
}

function isUnsafeSourceOpenError(error: unknown): boolean {
  return Boolean(error && typeof error === "object" && "code" in error && error.code === "ELOOP");
}
