import os from "node:os";
import path from "node:path";
import { STATE_DIR_NAME } from "../config/defaults.js";

export function defaultStateHome(env: NodeJS.ProcessEnv = process.env): string {
  return normalizeStateHome(env.AGENT_CORE_HOME || path.join(os.homedir(), STATE_DIR_NAME));
}

export function normalizeStateHome(input: string): string {
  return path.resolve(expandHome(input));
}

export function expandHome(input: string, home = os.homedir()): string {
  if (input === "~") return home;
  if (input.startsWith("~/")) return path.join(home, input.slice(2));
  return input;
}
