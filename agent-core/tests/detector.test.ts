import assert from "node:assert/strict";
import test from "node:test";
import { runDoctor } from "../src/cli/doctor.js";
import { CoreNative } from "../src/native/core.js";
import { CommandResult, CommandRunner } from "../src/platform/shell.js";
import { ProviderDetector } from "../src/doctor/provider-detector.js";

class FakeRunner implements CommandRunner {
  async run(command: string, args: string[] = []): Promise<CommandResult> {
    const key = [command, ...args].join(" ");
    if (key === "which claude") return { code: 0, stdout: "/usr/local/bin/claude\n", stderr: "" };
    if (key === "/usr/local/bin/claude --version") return { code: 0, stdout: "2.1.121\n", stderr: "" };
    if (key === "which codex") return { code: 1, stdout: "", stderr: "" };
    if (key === "which sandbox-exec") return { code: 0, stdout: "/usr/bin/sandbox-exec\n", stderr: "" };
    return { code: 1, stdout: "", stderr: "not found" };
  }
}

class CrashingProviderRunner implements CommandRunner {
  async run(command: string, args: string[] = []): Promise<CommandResult> {
    const key = [command, ...args].join(" ");
    if (key === "which claude") return { code: 0, stdout: "/usr/local/bin/claude\n", stderr: "" };
    if (key === "which codex") return { code: 0, stdout: "/usr/local/bin/codex\n", stderr: "" };
    if (key === "/usr/local/bin/claude --version") return { code: 1, stdout: "", stderr: "broken" };
    if (key === "/usr/local/bin/codex --version") return { code: 1, stdout: "", stderr: "broken" };
    if (key === "which sandbox-exec") return { code: 0, stdout: "/usr/bin/sandbox-exec\n", stderr: "" };
    return { code: 1, stdout: "", stderr: "not found" };
  }
}

test("ProviderDetector returns structured provider checks", async () => {
  const report = await new ProviderDetector(new FakeRunner(), {
    serverUrl: "https://api.example.test",
    networkChecker: async () => ({ reachable: true }),
  }).detect();

  assert.equal(report.server.url, "https://api.example.test");
  assert.equal(report.server.status, "reachable");
  assert.equal(report.server.reachable, true);

  assert.equal(report.providers.claude.installed, true);
  assert.equal(report.providers.claude.ready, true);
  assert.equal(report.providers.claude.path, "/usr/local/bin/claude");
  assert.equal(report.providers.claude.version, "2.1.121");

  assert.equal(report.providers.codex.installed, false);
  assert.equal(report.providers.codex.ready, false);
  assert.equal(report.providers.codex.reason, "not_found");

  if (process.platform === "darwin") {
    assert.equal(report.sandbox.supported, true);
    assert.equal(report.sandbox.provider, "sandbox-exec");
  } else {
    assert.equal(report.sandbox.supported, false);
    assert.equal(report.sandbox.reason, "not_macos");
  }
});

test("ProviderDetector reports crashed provider probes as actionable not ready checks", async () => {
  const report = await new ProviderDetector(new CrashingProviderRunner(), {
    serverUrl: "https://api.example.test",
    networkChecker: async () => ({ reachable: true }),
  }).detect();

  assert.equal(report.providers.claude.installed, true);
  assert.equal(report.providers.claude.ready, false);
  assert.equal(report.providers.claude.reason, "crashed");
  assert.match(report.providers.claude.fixCommand ?? "", /claude --version/);
  assert.equal(report.providers.claude.authStatus, undefined);

  assert.equal(report.providers.codex.installed, true);
  assert.equal(report.providers.codex.ready, false);
  assert.equal(report.providers.codex.reason, "crashed");
  assert.match(report.providers.codex.fixCommand ?? "", /codex --version/);
  assert.equal(report.providers.codex.authStatus, undefined);
});

test("ProviderDetector can skip server connectivity checks for isolated tests", async () => {
  const report = await new ProviderDetector(new FakeRunner(), {
    serverUrl: "https://api.example.test",
    checkServer: false,
  }).detect();

  assert.equal(report.server.status, "skipped");
  assert.equal(report.server.reachable, false);
  assert.match(report.server.reason ?? "", /disabled/);
});

test("ProviderDetector defaults to the production API URL", async () => {
  const previousServerUrl = process.env.AGENT_CORE_SERVER_URL;
  delete process.env.AGENT_CORE_SERVER_URL;

  try {
    let checkedUrl = "";
    const report = await new ProviderDetector(new FakeRunner(), {
      networkChecker: async (url) => {
        checkedUrl = url;
        return { reachable: true };
      },
    }).detect();

    assert.equal(checkedUrl, "https://api.puffo.ai");
    assert.equal(report.server.url, "https://api.puffo.ai");
    assert.equal(report.server.status, "reachable");
  } finally {
    if (previousServerUrl === undefined) {
      delete process.env.AGENT_CORE_SERVER_URL;
    } else {
      process.env.AGENT_CORE_SERVER_URL = previousServerUrl;
    }
  }
});

test("runDoctor redacts native core errors", async () => {
  const lines: string[] = [];

  await runDoctor({
    detector: new ProviderDetector(new FakeRunner(), {
      serverUrl: "https://api.example.test",
      checkServer: false,
    }),
    core: new SecretDoctorCoreNative(),
    log: (line) => lines.push(line),
  });

  const report = JSON.parse(lines[0] ?? "{}");
  assert.equal(report.core.status, "unavailable");
  assert.doesNotMatch(report.core.reason, /secret-value/);
  assert.match(report.core.reason, /token=\[redacted\]/);
});

class SecretDoctorCoreNative implements CoreNative {
  async openOrCreateDevice(): Promise<never> {
    throw new Error("doctor failed token=secret-value");
  }

  async startPairing(): Promise<never> {
    throw new Error("unused");
  }

  async confirmPairing(): Promise<never> {
    throw new Error("unused");
  }

  async openAgentSession(): Promise<never> {
    throw new Error("unused");
  }

  async syncOnce(): Promise<never> {
    throw new Error("unused");
  }

  async processPendingMessages(): Promise<never> {
    throw new Error("unused");
  }

  async sendChannelReply(): Promise<never> {
    throw new Error("unused");
  }

  async sendDirectReply(): Promise<never> {
    throw new Error("unused");
  }

  async snapshot(): Promise<never> {
    throw new Error("unused");
  }

  async closeSession(): Promise<void> {}
}
