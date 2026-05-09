import assert from "node:assert/strict";
import path from "node:path";
import test from "node:test";
import { CommandResult, CommandRunner } from "../src/platform/shell.js";
import { DefaultCommandRunner } from "../src/platform/shell.js";
import { assertSandboxAvailable, buildDarwinSandboxProfile } from "../src/policy/sandbox.js";
import { ResolvedPolicy } from "../src/policy/policy.js";
import { sidecarLaunchCommand } from "../src/native/sidecar-core.js";
import { classifyClaudeError } from "../src/providers/claude/claude-errors.js";
import { buildClaudePrompt, claudePrintArgs } from "../src/providers/claude/claude-command.js";
import { ClaudeSession } from "../src/providers/claude/claude-session.js";
import { buildCodexPrompt, codexExecArgs } from "../src/providers/codex/codex-command.js";
import { classifyCodexError } from "../src/providers/codex/codex-errors.js";
import { extractCodexSessionId, normalizeCodexOutput } from "../src/providers/codex/codex-stream.js";
import { CodexSession } from "../src/providers/codex/codex-session.js";
import { CliProcess } from "../src/providers/process/child-process.js";

test("Claude command builder encodes mustRespond metadata", () => {
  const prompt = buildClaudePrompt({
    body: "hello",
    instructions: "reply briefly",
    mustRespond: true,
    mentioned: true,
    dm: false,
    senderSlug: "sam",
    channelId: "channel-1",
  });
  assert.match(prompt, /mustRespond: true/);
  assert.match(prompt, /reply briefly/);
  assert.match(prompt, /mentioned: true/);
  assert.match(prompt, /sender: sam/);
  assert.match(prompt, /channel: channel-1/);
  assert.deepEqual(claudePrintArgs("hi").slice(0, 4), [
    "--print",
    "--output-format",
    "text",
    "--permission-mode",
  ]);
});

test("Codex command builder uses non-interactive json exec", () => {
  const prompt = buildCodexPrompt({
    body: "hello",
    instructions: "",
    mustRespond: false,
    mentioned: false,
    dm: false,
    senderSlug: "sam",
  });
  assert.match(prompt, /\[SILENT\]/);
  assert.match(prompt, /mentioned: false/);
  assert.match(prompt, /sender: sam/);
  assert.deepEqual(codexExecArgs("hi", "/tmp").slice(0, 4), ["exec", "--json", "--cd", "/tmp"]);
  assert.deepEqual(codexExecArgs("hi", "/tmp", "codex-session-1").slice(0, 4), [
    "exec",
    "resume",
    "--json",
    "--skip-git-repo-check",
  ]);
});

test("Codex output parser extracts text from common event shapes", () => {
  const stdout = [
    JSON.stringify({ type: "session.started", id: "abc" }),
    JSON.stringify({ type: "message", message: { type: "text", text: "hello" } }),
    JSON.stringify({ type: "response.output_item.done", item: { content: [{ type: "output_text", text: "world" }] } }),
    JSON.stringify({ result: "done" }),
  ].join("\n");

  assert.equal(normalizeCodexOutput(stdout), "hello\nworld\ndone");
  assert.equal(extractCodexSessionId(stdout), "abc");
  assert.equal(
    extractCodexSessionId(JSON.stringify({ type: "event", session: { id: "nested-session" } })),
    "nested-session",
  );
});

test("CodexSession captures and resumes Codex exec session ids", async () => {
  const runner = new QueuedRunner([
    {
      code: 0,
      stdout: [JSON.stringify({ type: "session.started", id: "codex-session-1" }), JSON.stringify({ result: "first" })].join("\n"),
      stderr: "",
    },
    {
      code: 0,
      stdout: JSON.stringify({ result: "second" }),
      stderr: "",
    },
  ]);
  const session = new CodexSession(testPolicy(), new CliProcess(runner));
  await session.start();

  const first = await session.send({
    body: "hello",
    instructions: "",
    mustRespond: true,
    mentioned: true,
    dm: false,
  });
  const second = await session.send({
    body: "again",
    instructions: "",
    mustRespond: true,
    mentioned: true,
    dm: false,
  });

  assert.equal(first.body, "first");
  assert.equal(second.body, "second");
  assert.equal(session.getStatus().sessionId, "codex-session-1");
  assert.equal(runner.calls[0]?.args[0], "exec");
  assert.equal(runner.calls[1]?.args[0], "exec");
  assert.equal(runner.calls[1]?.args[1], "resume");
  assert.equal(runner.calls[1]?.args.includes("codex-session-1"), true);
});

test("CodexSession retains session id in error status", async () => {
  const runner = new QueuedRunner([{ code: 1, stdout: "", stderr: "auth failed" }]);
  const session = new CodexSession(testPolicy(), new CliProcess(runner), "codex-session-1");
  await session.start();

  await session.send({
    body: "hello",
    instructions: "",
    mustRespond: true,
    mentioned: true,
    dm: false,
  });

  assert.equal(session.getStatus().state, "error");
  assert.equal(session.getStatus().sessionId, "codex-session-1");
});

test("ClaudeSession retains session id in error status", async () => {
  const runner = new QueuedRunner([{ code: 1, stdout: "", stderr: "auth failed" }]);
  const session = new ClaudeSession(testPolicy(), new CliProcess(runner), "00000000-0000-4000-8000-000000000001");
  await session.start();

  await session.send({
    body: "hello",
    instructions: "",
    mustRespond: true,
    mentioned: true,
    dm: false,
  });

  assert.equal(session.getStatus().state, "error");
  assert.equal(session.getStatus().sessionId, "00000000-0000-4000-8000-000000000001");
});

test("Provider sessions run resolved executable paths", async () => {
  const codexRunner = new QueuedRunner([{ code: 0, stdout: JSON.stringify({ result: "ok" }), stderr: "" }]);
  const claudeRunner = new QueuedRunner([{ code: 0, stdout: "ok", stderr: "" }]);
  const codex = new CodexSession(testPolicy(), new CliProcess(codexRunner), undefined, "/opt/tools/codex");
  const claude = new ClaudeSession(testPolicy(), new CliProcess(claudeRunner), undefined, "/opt/tools/claude");
  await codex.start();
  await claude.start();

  await codex.send({
    body: "hello",
    instructions: "",
    mustRespond: true,
    mentioned: true,
    dm: false,
  });
  await claude.send({
    body: "hello",
    instructions: "",
    mustRespond: true,
    mentioned: true,
    dm: false,
  });

  assert.equal(codexRunner.calls[0]?.command, "/opt/tools/codex");
  assert.equal(claudeRunner.calls[0]?.command, "/opt/tools/claude");
});

test("Darwin sandbox profile grants declared roots only", () => {
  const profile = buildDarwinSandboxProfile({
    enabled: true,
    platform: "darwin",
    network: "deny",
    readableRoots: ["/usr/bin", "/tmp/agent-home"],
    writableRoots: ["/tmp/agent-home"],
    deniedExecutables: ["/usr/bin/security", "/bin/ps"],
  });

  assert.match(profile, /\(deny default\)/);
  assert.match(profile, /\(deny process-exec \(literal "\/usr\/bin\/security"\) \(literal "\/bin\/ps"\)\)/);
  assert.match(profile, /\(allow file-read\* \(subpath "\/usr\/bin"\)/);
  assert.match(profile, /\(allow file-write\* \(subpath "\/tmp\/agent-home"\)\)/);
  assert.doesNotMatch(profile, /\(allow network\*\)/);
});

test("CliProcess wraps provider command with sandbox-exec when policy enables sandbox", async () => {
  const runner = new RecordingRunner();
  const process = new CliProcess(runner);
  const policy: ResolvedPolicy = {
    accessMode: "safe",
    cwd: "/tmp/workspace",
    env: {},
    agentHome: "/tmp/home",
    workspace: "/tmp/workspace",
    networkAccess: "inherit",
    deniedTools: [],
    fileAccess: { readablePaths: [], writablePaths: [] },
    sandbox: {
      enabled: true,
      platform: "darwin",
      network: "inherit",
      readableRoots: ["/usr/bin", "/tmp/home", "/tmp/workspace"],
      writableRoots: ["/tmp/home", "/tmp/workspace"],
      deniedExecutables: ["/usr/bin/security"],
    },
  };

  await process.runText("codex", ["exec", "hello"], policy);

  assert.equal(path.basename(runner.command), "sandbox-exec");
  assert.equal(runner.args[0], "-p");
  assert.match(runner.args[1] ?? "", /\(deny default\)/);
  assert.equal(runner.args.at(-3), "codex");
  assert.equal(runner.args.at(-2), "exec");
  assert.equal(runner.args.at(-1), "hello");
});

test("sandbox availability validation reports a local sandbox setup error", { skip: process.platform !== "darwin" }, () => {
  assert.throws(
    () => assertSandboxAvailable(testSandboxPolicy(), () => false),
    /sandbox_unavailable: sandbox-exec is required/,
  );
  assert.doesNotThrow(() => assertSandboxAvailable(testSandboxPolicy(), () => true));
});

test("sidecar launch prefers packaged native binary and falls back to cargo", () => {
  const packaged = sidecarLaunchCommand({
    manifestPath: "/tmp/agent-native/Cargo.toml",
    features: ["dev-tools"],
    binaryPath: "/tmp/bin/agent-native-sidecar",
  });
  assert.equal(packaged.command, "/tmp/bin/agent-native-sidecar");
  assert.deepEqual(packaged.args, []);

  const manifestPath = path.join(process.cwd(), "crates", "agent-native", "Cargo.toml");
  const cargo = sidecarLaunchCommand({
    manifestPath,
    features: ["dev-tools"],
  });
  assert.equal(cargo.command, "cargo");
  assert.deepEqual(cargo.args.slice(0, 4), ["run", "--quiet", "--manifest-path", manifestPath]);
  assert.match(cargo.args.join(" "), /agent-native-sidecar/);
});

test("provider command errors classify missing executables", async () => {
  assert.equal(classifyClaudeError("spawn claude ENOENT"), "claude_not_found");
  assert.equal(classifyCodexError("spawn codex ENOENT"), "codex_not_found");
  assert.equal(classifyClaudeError("spawn sandbox-exec ENOENT"), "sandbox_unavailable");
  assert.equal(classifyCodexError("spawn sandbox-exec ENOENT"), "sandbox_unavailable");

  const result = await new DefaultCommandRunner().run("agent-core-command-that-does-not-exist", [], {
    timeoutMs: 100,
  });
  assert.notEqual(result.code, 0);
  assert.match(result.stderr, /ENOENT|no such file/i);
});

class RecordingRunner implements CommandRunner {
  command = "";
  args: string[] = [];

  async run(command: string, args: string[] = []): Promise<CommandResult> {
    this.command = command;
    this.args = args;
    return { code: 0, stdout: "ok", stderr: "" };
  }
}

class QueuedRunner implements CommandRunner {
  readonly calls: Array<{ command: string; args: string[] }> = [];

  constructor(private readonly results: CommandResult[]) {}

  async run(command: string, args: string[] = []): Promise<CommandResult> {
    this.calls.push({ command, args });
    return this.results.shift() ?? { code: 0, stdout: "", stderr: "" };
  }
}

function testPolicy(): ResolvedPolicy {
  return {
    accessMode: "safe",
    cwd: "/tmp/workspace",
    env: {},
    agentHome: "/tmp/home",
    workspace: "/tmp/workspace",
    networkAccess: "inherit",
    deniedTools: [],
    fileAccess: { readablePaths: [], writablePaths: [] },
  };
}

function testSandboxPolicy(): ResolvedPolicy {
  return {
    ...testPolicy(),
    sandbox: {
      enabled: true,
      platform: "darwin",
      network: "inherit",
      readableRoots: ["/usr/bin", "/tmp/home", "/tmp/workspace"],
      writableRoots: ["/tmp/home", "/tmp/workspace"],
      deniedExecutables: [],
    },
  };
}
