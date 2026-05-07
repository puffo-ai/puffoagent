#!/usr/bin/env node
import { execFile } from "node:child_process";
import fs from "node:fs/promises";
import path from "node:path";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);
const packageRoot = process.cwd();
const submoduleRoot = path.join(packageRoot, "core");
const patchPath = path.resolve(packageRoot, "..", "docs", "patches", "agent-core-core-upstream.patch");
const patchBasePath = path.resolve(packageRoot, "..", "docs", "patches", "agent-core-core-upstream.base");

await assertDirectory(submoduleRoot, "core submodule");
await runGit(submoduleRoot, ["rev-parse", "--show-toplevel"]);
const baseOid = await readPatchBaseOid();
const { stdout: diff } = await runGit(submoduleRoot, ["diff", "--binary", baseOid]);
if (!diff.trim()) {
  throw new Error("agent-core/core has no diff from the recorded upstream patch base to export");
}

await fs.mkdir(path.dirname(patchPath), { recursive: true });
await fs.writeFile(patchPath, diff);

await execFileAsync(process.execPath, [path.join(packageRoot, "scripts", "check-core-upstream-patch.mjs")], {
  cwd: packageRoot,
  maxBuffer: 20 * 1024 * 1024,
});

const bytes = Buffer.byteLength(diff);
console.log(`exported core upstream patch: ${path.relative(packageRoot, patchPath)} (${bytes} bytes)`);

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
