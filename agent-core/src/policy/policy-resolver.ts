import fs from "node:fs/promises";
import path from "node:path";
import { AgentConfig } from "../types.js";
import { StateStore } from "../state/store.js";
import { ResolvedPolicy } from "./policy.js";
import { normalizeDeniedTools } from "./denied-tools.js";
import { FileAccessPolicy, normalizeFileAccess } from "./file-access.js";
import { parseNetworkAccess } from "./network-access.js";
import { projectProviderCredentials } from "./provider-credentials.js";
import { normalizeProviderConfigPathsForProvider } from "./provider-config-paths.js";
import { sandboxPolicyForResolvedPolicy } from "./sandbox.js";
import { ensureDirectory } from "./workspace-policy.js";

export interface PolicyResolverOptions {
  credentialSourceHome?: string;
  projectProviderCredentials?: boolean;
}

export class PolicyResolutionError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "PolicyResolutionError";
  }
}

export class PolicyResolver {
  constructor(
    private readonly store: StateStore,
    private readonly options: PolicyResolverOptions = {},
  ) {}

  async resolve(agent: AgentConfig): Promise<ResolvedPolicy> {
    return this.resolveInternal(agent, { materialize: true });
  }

  async preview(agent: AgentConfig): Promise<ResolvedPolicy> {
    return this.resolveInternal(agent, { materialize: false });
  }

  private async resolveInternal(
    agent: AgentConfig,
    options: { materialize: boolean },
  ): Promise<ResolvedPolicy> {
    const agentHome = path.join(this.store.paths.root, "agents", agent.id, "home");
    if (options.materialize) {
      await ensureDirectory(agentHome, { root: this.store.paths.root });
      await ensureDirectory(agent.workspace, { root: this.store.paths.root });
    } else {
      await assertPreviewPathWithinRoot(agentHome, this.store.paths.root);
      await assertPreviewPathWithinRoot(agent.workspace, this.store.paths.root);
    }
    const networkAccess = parseNetworkAccess(agent.networkAccess);
    const deniedTools = normalizeDeniedTools(agent.deniedTools);
    const fileAccess = await canonicalFileAccess(agent.fileAccess);
    if (options.materialize && agent.accessMode !== "trusted") {
      await projectProviderCredentials(agent.provider, agentHome, credentialProjectionOptions(this.options, agent));
    }

    const baseEnv = baseProviderEnv(agent.accessMode);

    const env: NodeJS.ProcessEnv =
      agent.accessMode === "trusted"
        ? {
            ...baseEnv,
            AGENT_ID: agent.id,
          }
        : {
            ...baseEnv,
            HOME: agentHome,
            USERPROFILE: agentHome,
            AGENT_ID: agent.id,
          };

    if (agent.provider === "codex" && agent.accessMode !== "trusted") {
      env.CODEX_HOME = path.join(agentHome, ".codex");
    }

    if (agent.accessMode === "project") {
      if (!agent.projectPath) throw new Error("projectPath is required when accessMode is project");
      const projectPath = await canonicalProjectPath(agent.projectPath);
      return withSandbox({
        accessMode: agent.accessMode,
        cwd: projectPath,
        env,
        agentHome,
        workspace: agent.workspace,
        networkAccess,
        deniedTools,
        fileAccess,
        projectPath,
      });
    }

    if (agent.accessMode === "trusted") {
      const projectPath = agent.projectPath ? await canonicalProjectPath(agent.projectPath) : undefined;
      const cwd = projectPath || process.cwd();
      return withSandbox({
        accessMode: agent.accessMode,
        cwd,
        env,
        agentHome,
        workspace: agent.workspace,
        networkAccess,
        deniedTools,
        fileAccess,
        ...(projectPath ? { projectPath } : {}),
      });
    }

    return withSandbox({
      accessMode: "safe",
      cwd: agent.workspace,
      env,
      agentHome,
      workspace: agent.workspace,
      networkAccess,
      deniedTools,
      fileAccess,
    });
  }
}

function credentialProjectionOptions(options: PolicyResolverOptions, agent: AgentConfig) {
  return {
    ...(options.credentialSourceHome ? { sourceHome: options.credentialSourceHome } : {}),
    ...(options.projectProviderCredentials !== undefined ? { enabled: options.projectProviderCredentials } : {}),
    extraPaths: normalizeProviderConfigPathsForProvider(agent.provider, agent.providerConfigPaths),
  };
}

function baseProviderEnv(accessMode: AgentConfig["accessMode"]): NodeJS.ProcessEnv {
  if (accessMode !== "trusted") {
    return {
      PATH: process.env.PATH,
      LANG: process.env.LANG,
      LC_ALL: process.env.LC_ALL,
    };
  }
  return Object.fromEntries(
    Object.entries(process.env).filter(([key]) => !key.startsWith("AGENT_CORE_")),
  );
}

function withSandbox(policy: Omit<ResolvedPolicy, "sandbox">): ResolvedPolicy {
  const sandbox = sandboxPolicyForResolvedPolicy(policy);
  return sandbox ? { ...policy, sandbox } : policy;
}

async function canonicalProjectPath(projectPath: string): Promise<string> {
  if (!path.isAbsolute(projectPath)) {
    throw new PolicyResolutionError("projectPath must be absolute");
  }
  try {
    const realPath = await fs.realpath(projectPath);
    const stat = await fs.stat(realPath);
    if (!stat.isDirectory()) throw new PolicyResolutionError("projectPath must be an existing directory");
    return realPath;
  } catch (error) {
    if (error instanceof PolicyResolutionError) throw error;
    throw new PolicyResolutionError("projectPath must be an existing directory");
  }
}

async function canonicalFileAccess(value: unknown): Promise<FileAccessPolicy> {
  const access = normalizeFileAccess(value);
  return {
    readablePaths: await canonicalDirectoryList(access.readablePaths, "fileAccess.readablePaths"),
    writablePaths: await canonicalDirectoryList(access.writablePaths, "fileAccess.writablePaths"),
  };
}

async function canonicalDirectoryList(values: string[], label: string): Promise<string[]> {
  const paths = await Promise.all(values.map((value) => canonicalFileAccessPath(value, label)));
  return Array.from(new Set(paths));
}

async function canonicalFileAccessPath(value: string, label: string): Promise<string> {
  if (!path.isAbsolute(value)) {
    throw new PolicyResolutionError(`${label} entries must be absolute paths`);
  }
  try {
    const realPath = await fs.realpath(value);
    const stat = await fs.stat(realPath);
    if (!stat.isDirectory()) throw new PolicyResolutionError(`${label} entries must be existing directories`);
    return realPath;
  } catch (error) {
    if (error instanceof PolicyResolutionError) throw error;
    throw new PolicyResolutionError(`${label} entries must be existing directories`);
  }
}

async function assertPreviewPathWithinRoot(pathname: string, root: string): Promise<void> {
  const target = path.resolve(pathname);
  const resolvedRoot = path.resolve(root);
  const relative = path.relative(resolvedRoot, target);
  if (relative.startsWith("..") || path.isAbsolute(relative)) {
    throw new PolicyResolutionError(`unsafe filesystem path: ${target}`);
  }

  let current = resolvedRoot;
  await assertPreviewDirectoryIfPresent(current);
  for (const segment of relative.split(path.sep).filter(Boolean)) {
    current = path.join(current, segment);
    const exists = await assertPreviewDirectoryIfPresent(current);
    if (!exists) return;
  }
}

async function assertPreviewDirectoryIfPresent(pathname: string): Promise<boolean> {
  try {
    const stat = await fs.lstat(pathname);
    if (stat.isSymbolicLink() || !stat.isDirectory()) {
      throw new PolicyResolutionError(`unsafe filesystem path: ${pathname}`);
    }
    return true;
  } catch (error) {
    if (error instanceof PolicyResolutionError) throw error;
    if (error && typeof error === "object" && "code" in error && error.code === "ENOENT") return false;
    throw error;
  }
}
