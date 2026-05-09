import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { CliCoreNative } from "../src/native/cli-core.js";

test("CliCoreNative validates agent identity and session inputs before native command spawn", async () => {
  const core = new CliCoreNative();
  await assert.rejects(
    core.createAgentIdentity({ operatorSlug: "Alice", agentSlug: "alice-agent" }),
    /operatorSlug must be a lowercase slug/,
  );
  await assert.rejects(
    core.createAgentIdentity({ operatorSlug: "alice", agentSlug: "bad slug" }),
    /agentSlug must be a lowercase slug/,
  );
  await assert.rejects(
    core.openAgentSession(null as unknown as Record<string, unknown>),
    /openAgentSession input must be an object/,
  );
  await assert.rejects(
    core.openAgentSession({ agentSlug: "bad slug" }),
    /agentSlug must be a lowercase slug/,
  );
});

test("CliCoreNative validates message RPC inputs before native command spawn", async () => {
  const core = new CliCoreNative();
  await assert.rejects(
    core.processPendingMessages("" as any),
    /native session handle is required/,
  );
  await assert.rejects(
    core.sendChannelReply("handle" as any, null as unknown as Record<string, unknown>),
    /sendChannelReply input must be an object/,
  );
  await assert.rejects(
    core.sendDirectReply("handle" as any, { recipientSlug: "Alice", body: "hello" }),
    /recipientSlug must be a lowercase slug/,
  );
  await assert.rejects(
    core.snapshot("" as any),
    /native session handle is required/,
  );
  await assert.rejects(
    core.closeSession("" as any),
    /native session handle is required/,
  );
});

test("CliCoreNative rejects malformed native command response shapes", { skip: process.platform === "win32" }, async () => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-cli-core-cargo-"));
  const cargo = path.join(dir, "cargo");
  await fs.writeFile(
    cargo,
    [
      "#!/usr/bin/env node",
      "const args = process.argv.slice(2);",
      "if (args.includes('dev-create-agent-identity')) {",
      "  const agent = args[args.indexOf('--agent') + 1];",
      "  if (agent === 'oversized-agent') {",
      "    console.log(JSON.stringify({ ok: true, operator_slug: 'alice', agent_slug: 'oversized-agent', identity_type: 'agent', declared_operator_public_key: 'k'.repeat(4097) }));",
      "  } else {",
      "    console.log(JSON.stringify({ ok: true, operator_slug: 'alice', agent_slug: 'alice-agent', identity_type: 'agent' }));",
      "  }",
      "} else {",
      "  console.log(JSON.stringify({}));",
      "}",
    ].join("\n"),
    { mode: 0o700 },
  );
  const previousPath = process.env.PATH;
  process.env.PATH = `${dir}${path.delimiter}${previousPath ?? ""}`;
  const core = new CliCoreNative();
  try {
    await assert.rejects(
      core.openOrCreateDevice(),
      /native health response\.ok must be boolean/,
    );
    await assert.rejects(
      core.createAgentIdentity({ operatorSlug: "alice", agentSlug: "alice-agent" }),
      /declared_operator_public_key is required/,
    );
    await assert.rejects(
      core.createAgentIdentity({ operatorSlug: "alice", agentSlug: "oversized-agent" }),
      /declared_operator_public_key.*4096/,
    );
    await assert.rejects(
      core.openAgentSession({ agentSlug: "alice-agent" }),
      /handle is required/,
    );
    await assert.rejects(
      core.sendChannelReply("handle" as any, { spaceId: "space", channelId: "channel", body: "hello" }),
      /message_id is required/,
    );
  } finally {
    if (previousPath === undefined) delete process.env.PATH;
    else process.env.PATH = previousPath;
    await fs.rm(dir, { recursive: true, force: true });
  }
});
