import fs from "node:fs/promises";
import path from "node:path";

export interface EnsureDirectoryOptions {
  root?: string;
}

export async function ensureDirectory(pathname: string, options: EnsureDirectoryOptions = {}): Promise<void> {
  const target = path.resolve(pathname);
  if (options.root) {
    await ensureDirectoryUnderRoot(target, path.resolve(options.root));
    return;
  }
  await fs.mkdir(target, { recursive: true, mode: 0o700 });
  await assertSafeDirectory(target);
  await fs.chmod(target, 0o700).catch(() => undefined);
}

export async function assertSafeRegularFile(pathname: string): Promise<void> {
  const stat = await fs.lstat(pathname);
  if (stat.isSymbolicLink() || !stat.isFile()) {
    throw unsafePathError(pathname);
  }
}

export async function assertDirectoryWithinRoot(pathname: string, root: string): Promise<void> {
  const target = path.resolve(pathname);
  const resolvedRoot = path.resolve(root);
  const relative = path.relative(resolvedRoot, target);
  if (relative.startsWith("..") || path.isAbsolute(relative)) {
    throw unsafePathError(target);
  }

  await assertSafeDirectory(resolvedRoot);
  let current = resolvedRoot;
  for (const segment of relative.split(path.sep).filter(Boolean)) {
    current = path.join(current, segment);
    await assertSafeDirectory(current);
  }
}

export function isNotFound(error: unknown): boolean {
  return Boolean(error && typeof error === "object" && "code" in error && error.code === "ENOENT");
}

export function unsafePathError(pathname: string): Error {
  return new Error(`unsafe filesystem path: ${pathname}`);
}

async function ensureDirectoryUnderRoot(target: string, root: string): Promise<void> {
  const relative = path.relative(root, target);
  if (relative.startsWith("..") || path.isAbsolute(relative)) {
    throw unsafePathError(target);
  }

  await fs.mkdir(root, { recursive: true, mode: 0o700 });
  await assertSafeDirectory(root);
  await fs.chmod(root, 0o700).catch(() => undefined);

  let current = root;
  for (const segment of relative.split(path.sep).filter(Boolean)) {
    current = path.join(current, segment);
    try {
      await assertSafeDirectory(current);
    } catch (error) {
      if (!isNotFound(error)) throw error;
      await fs.mkdir(current, { mode: 0o700 });
      await assertSafeDirectory(current);
    }
    await fs.chmod(current, 0o700).catch(() => undefined);
  }
}

async function assertSafeDirectory(pathname: string): Promise<void> {
  const stat = await fs.lstat(pathname);
  if (stat.isSymbolicLink() || !stat.isDirectory()) {
    throw unsafePathError(pathname);
  }
}
