#!/usr/bin/env node
import { execFile } from "node:child_process";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);
const packageRoot = process.cwd();
const submoduleRoot = path.join(packageRoot, "core");
const patchPath = path.resolve(packageRoot, "..", "docs", "patches", "agent-core-core-upstream.patch");
const patchBasePath = path.resolve(packageRoot, "..", "docs", "patches", "agent-core-core-upstream.base");
let worktree;

await assertFile(patchPath);
await assertDirectory(submoduleRoot, "core submodule");
await runGit(submoduleRoot, ["rev-parse", "--show-toplevel"]);
const baseOid = await readPatchBaseOid();
const patch = await fs.readFile(patchPath, "utf8");
const { stdout: currentDiffFromBase } = await runGit(submoduleRoot, ["diff", "--binary", baseOid]);
if (currentDiffFromBase.trim() && currentDiffFromBase !== patch) {
  throw new Error(
    [
      "agent-core/core differs from the parent submodule pointer, but the exported core patch does not match that diff.",
      "Run npm run export:core-patch from agent-core/ after reviewing the submodule changes.",
    ].join("\n"),
  );
}

try {
  if (currentDiffFromBase.trim()) {
    await runGit(submoduleRoot, ["apply", "--check", "--reverse", patchPath]);
  }
  worktree = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-core-patch-check-"));
  await runGit(submoduleRoot, ["worktree", "add", "--detach", worktree, baseOid]);
  await runGit(worktree, ["apply", "--check", patchPath]);
  console.log(`core upstream patch ok: ${path.relative(packageRoot, patchPath)}`);
} finally {
  if (worktree) {
    await runGit(submoduleRoot, ["worktree", "remove", "--force", worktree]).catch(() => undefined);
    await fs.rm(worktree, { recursive: true, force: true }).catch(() => undefined);
  }
}

async function readParentGitlinkOid() {
  const { stdout } = await runGit(packageRoot, ["ls-files", "-s", "--", "core"]);
  const match = stdout.match(/^160000\s+([0-9a-f]{40})\s+\d+\s+core$/m);
  if (!match) {
    throw new Error(
      `missing core submodule gitlink in parent index. Run git submodule update --init agent-core/core from the repository root.`,
    );
  }
  return match[1];
}

async function readPatchBaseOid() {
  const base = await fs.readFile(patchBasePath, "utf8").catch((error) => {
    if (error?.code === "ENOENT") return undefined;
    throw error;
  });
  if (base === undefined) return readParentGitlinkOid();
  const oid = base.trim();
  if (!/^[0-9a-f]{40}$/.test(oid)) {
    throw new Error(`invalid core upstream patch base OID in ${patchBasePath}`);
  }
  return oid;
}

async function assertFile(filePath) {
  const stat = await fs.stat(filePath).catch(() => undefined);
  if (!stat?.isFile()) throw new Error(`missing upstream patch file: ${filePath}`);
}

async function assertDirectory(directoryPath, label) {
  const stat = await fs.stat(directoryPath).catch(() => undefined);
  if (!stat?.isDirectory()) {
    throw new Error(
      `missing ${label}: ${directoryPath}. Run git submodule update --init agent-core/core from the repository root.`,
    );
  }
}

async function runGit(cwd, args) {
  try {
    return await execFileAsync("git", args, {
      cwd,
      maxBuffer: 20 * 1024 * 1024,
    });
  } catch (error) {
    const failed = error;
    const stderr = failed.stderr ? `\n${failed.stderr}` : "";
    const stdout = failed.stdout ? `\n${failed.stdout}` : "";
    throw new Error(`git ${args.join(" ")} failed in ${cwd}${stdout}${stderr}`);
  }
}
