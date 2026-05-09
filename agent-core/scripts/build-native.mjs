#!/usr/bin/env node
import { spawn } from "node:child_process";
import fs from "node:fs/promises";
import path from "node:path";

const executable = process.platform === "win32" ? "agent-native-sidecar.exe" : "agent-native-sidecar";
const packageRoot = process.cwd();
const profile = nativeBuildProfile();
const features = profile === "prod" ? ["apple-keychain"] : ["dev-tools"];

await run("cargo", [
  "build",
  "--release",
  "--manifest-path",
  path.join("crates", "agent-native", "Cargo.toml"),
  "--features",
  features.join(","),
  "--bin",
  "agent-native-sidecar",
]);

const source = path.join(packageRoot, "crates", "agent-native", "target", "release", executable);
const targetDir = path.join(packageRoot, "bin", process.platform, process.arch);
const target = path.join(targetDir, executable);

await fs.mkdir(targetDir, { recursive: true });
await fs.copyFile(source, target);
await fs.chmod(target, 0o755).catch(() => undefined);
console.error(
  `staged native sidecar (${profile}, features=${features.join(",")}): ${path.relative(packageRoot, target)}`,
);

function nativeBuildProfile() {
  const argProfile = process.argv.find((arg) => arg.startsWith("--profile="))?.slice("--profile=".length);
  const profileFlag = process.argv.indexOf("--profile");
  const flagProfile = profileFlag >= 0 ? process.argv[profileFlag + 1] : undefined;
  const value = argProfile ?? flagProfile ?? process.env.AGENT_CORE_NATIVE_BUILD_PROFILE ?? "dev";
  if (value === "dev" || value === "prod") return value;
  throw new Error(`unsupported native build profile: ${value}; expected dev or prod`);
}

function run(command, args) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, {
      cwd: packageRoot,
      stdio: "inherit",
    });
    child.on("error", reject);
    child.on("exit", (code, signal) => {
      if (code === 0) return resolve();
      reject(new Error(`${command} ${args.join(" ")} failed code=${code ?? "null"} signal=${signal ?? "null"}`));
    });
  });
}
