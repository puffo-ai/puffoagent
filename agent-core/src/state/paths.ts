import path from "node:path";
import { defaultStateHome, normalizeStateHome } from "../platform/paths.js";

export interface StatePaths {
  root: string;
  daemonFile: string;
  deviceFile: string;
  agentsDir: string;
}

export function statePaths(root = defaultStateHome()): StatePaths {
  const normalizedRoot = normalizeStateHome(root);
  return {
    root: normalizedRoot,
    daemonFile: path.join(normalizedRoot, "daemon.json"),
    deviceFile: path.join(normalizedRoot, "device.json"),
    agentsDir: path.join(normalizedRoot, "agents"),
  };
}

export function agentDir(root: string, agentId: string): string {
  return path.join(root, "agents", agentId);
}

export function agentWorkspace(root: string, agentId: string): string {
  return path.join(agentDir(root, agentId), "workspace");
}
