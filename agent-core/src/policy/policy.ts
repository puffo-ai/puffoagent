import { AccessMode } from "../types.js";
import { FileAccessPolicy } from "./file-access.js";
import { NetworkAccess } from "./network-access.js";

export interface ResolvedPolicy {
  accessMode: AccessMode;
  cwd: string;
  env: NodeJS.ProcessEnv;
  agentHome: string;
  workspace: string;
  projectPath?: string;
  networkAccess: NetworkAccess;
  deniedTools: string[];
  fileAccess: FileAccessPolicy;
  sandbox?: SandboxPolicy;
}

export interface SandboxPolicy {
  enabled: boolean;
  platform: "darwin";
  network: "inherit" | "deny";
  readableRoots: string[];
  writableRoots: string[];
  deniedExecutables: string[];
}
