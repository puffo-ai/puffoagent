import { CommandRunner, DefaultCommandRunner } from "../../platform/shell.js";
import { ResolvedPolicy } from "../../policy/policy.js";
import { applySandboxLaunch } from "../../policy/sandbox.js";

export class CliProcess {
  constructor(private readonly runner: CommandRunner = new DefaultCommandRunner()) {}

  async runText(
    command: string,
    args: string[],
    policy: ResolvedPolicy,
    input?: string,
  ): Promise<{ code: number | null; stdout: string; stderr: string }> {
    const options: {
      cwd: string;
      env: NodeJS.ProcessEnv;
      timeoutMs: number;
      input?: string;
    } = {
      cwd: policy.cwd,
      env: policy.env,
      timeoutMs: 10 * 60 * 1000,
    };
    if (input !== undefined) options.input = input;
    const launch = applySandboxLaunch(command, args, policy);
    return this.runner.run(launch.command, launch.args, options);
  }
}
