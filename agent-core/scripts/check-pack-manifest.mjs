#!/usr/bin/env node
import { execFile } from "node:child_process";
import { readFile } from "node:fs/promises";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);
const nativeExecutable = process.platform === "win32" ? "agent-native-sidecar.exe" : "agent-native-sidecar";
const expectedNativeSidecar = `bin/${process.platform}/${process.arch}/${nativeExecutable}`;
const requiredFiles = new Map([
  ["README.md", undefined],
  ["package.json", undefined],
  ["dist/src/cli/index.js", 0o755],
  [expectedNativeSidecar, 0o755],
]);
const forbiddenPrefixes = [
  "scripts/",
  "src/",
  "tests/",
  "crates/",
  "node_modules/",
  "dist/tests/",
  "target/",
];
const forbiddenExact = new Set(["package-lock.json", ".env"]);
const forbiddenSuffixes = [".tgz", ".sqlite", ".sqlite3", ".db", ".log"];
const forbiddenInfixes = [".sqlite-", ".sqlite3-", ".db-"];

const { stdout } = await execFileAsync("npm", ["pack", "--dry-run", "--json"], {
  maxBuffer: 20 * 1024 * 1024,
});
const [pack] = parsePackJson(stdout);
if (!pack || !Array.isArray(pack.files)) {
  throw new Error("npm pack --dry-run --json did not return a package file manifest");
}

const files = new Map(pack.files.map((file) => [file.path, file]));
for (const [filePath, mode] of requiredFiles) {
  const entry = files.get(filePath);
  if (!entry) throw new Error(`package is missing required file: ${filePath}`);
  if (mode !== undefined && entry.mode !== mode) {
    throw new Error(`package file has wrong mode: ${filePath} mode=${entry.mode}; expected ${mode}`);
  }
}

for (const filePath of files.keys()) {
  if (forbiddenExact.has(filePath)) {
    throw new Error(`package includes forbidden file: ${filePath}`);
  }
  if (filePath.startsWith(".env.")) {
    throw new Error(`package includes forbidden env file: ${filePath}`);
  }
  if (forbiddenPrefixes.some((prefix) => filePath.startsWith(prefix))) {
    throw new Error(`package includes forbidden source/development path: ${filePath}`);
  }
  if (forbiddenSuffixes.some((suffix) => filePath.endsWith(suffix))) {
    throw new Error(`package includes forbidden generated artifact: ${filePath}`);
  }
  if (forbiddenInfixes.some((infix) => filePath.includes(infix))) {
    throw new Error(`package includes forbidden generated artifact: ${filePath}`);
  }
}

const readme = await readFile("README.md", "utf8");
if (/\]\(\.\.\/docs\//.test(readme)) {
  throw new Error("README.md is published without repository docs/; avoid Markdown links to ../docs/");
}

console.log(`pack manifest ok: ${pack.entryCount} files, native sidecar ${expectedNativeSidecar}`);

function parsePackJson(output) {
  const trimmed = output.trim();
  const start = trimmed.lastIndexOf("\n[");
  const jsonText = start >= 0 ? trimmed.slice(start + 1) : trimmed;
  try {
    return JSON.parse(jsonText);
  } catch (error) {
    throw new Error(`could not parse npm pack JSON output: ${error instanceof Error ? error.message : String(error)}`);
  }
}
