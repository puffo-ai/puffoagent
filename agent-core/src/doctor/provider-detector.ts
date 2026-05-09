import { EnvironmentReport, ProviderCheck } from "../types.js";
import { DEFAULT_SERVER_URL } from "../config/defaults.js";
import { CommandRunner, DefaultCommandRunner } from "../platform/shell.js";
import { detectClaude } from "./claude-detector.js";
import { detectCodex } from "./codex-detector.js";
import { checkNetwork, NetworkCheck } from "./network-detector.js";
import { detectSandbox } from "./sandbox-detector.js";

export type NetworkChecker = (url: string, timeoutMs?: number) => Promise<NetworkCheck>;

export interface ProviderDetectorOptions {
  serverUrl?: string;
  checkServer?: boolean;
  networkChecker?: NetworkChecker;
}

export class ProviderDetector {
  constructor(
    private readonly runner: CommandRunner = new DefaultCommandRunner(),
    private readonly options: ProviderDetectorOptions = {},
  ) {}

  async detect(): Promise<EnvironmentReport> {
    const [claude, codex, sandbox, server] = await Promise.all([
      detectClaude(this.runner),
      detectCodex(this.runner),
      detectSandbox(this.runner),
      this.detectServer(),
    ]);
    return {
      os: process.platform,
      arch: process.arch,
      nodeVersion: process.version,
      server,
      sandbox,
      providers: { claude, codex },
    };
  }

  async detectOne(provider: "claude" | "codex"): Promise<ProviderCheck> {
    return provider === "claude" ? detectClaude(this.runner) : detectCodex(this.runner);
  }

  private async detectServer(): Promise<EnvironmentReport["server"]> {
    const url = this.options.serverUrl ?? process.env.AGENT_CORE_SERVER_URL ?? DEFAULT_SERVER_URL;
    if (this.options.checkServer === false) {
      return {
        url,
        status: "skipped",
        reachable: false,
        reason: "server connectivity check disabled",
      };
    }
    const checker = this.options.networkChecker ?? checkNetwork;
    const result = await checker(url);
    return {
      url,
      status: result.reachable ? "reachable" : "unreachable",
      reachable: result.reachable,
      ...(result.reason ? { reason: result.reason } : {}),
    };
  }
}
