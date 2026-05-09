import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { PolicyResolver } from "../src/policy/policy-resolver.js";
import { buildDarwinSandboxProfile } from "../src/policy/sandbox.js";
import { StateStore } from "../src/state/store.js";

test("PolicyResolver projects allowlisted Codex credentials into isolated home", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-policy-"));
  const sourceHome = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-source-home-"));
  await fs.mkdir(path.join(sourceHome, ".codex"), { recursive: true });
  await fs.writeFile(path.join(sourceHome, ".codex", "auth.json"), "{\"token\":\"test\"}\n");
  await fs.writeFile(path.join(sourceHome, ".codex", "config.toml"), "model = \"test\"\n");

  const store = new StateStore(root);
  await store.init();
  const agent = await store.createAgent({
    name: "Codex Agent",
    provider: "codex",
    accessMode: "safe",
  });

  const policy = await new PolicyResolver(store, { credentialSourceHome: sourceHome }).resolve(agent);
  const projectedAuth = path.join(policy.agentHome, ".codex", "auth.json");
  const projectedConfig = path.join(policy.agentHome, ".codex", "config.toml");

  assert.equal(policy.env.CODEX_HOME, path.join(policy.agentHome, ".codex"));
  assert.equal(await fs.readFile(projectedAuth, "utf8"), "{\"token\":\"test\"}\n");
  assert.equal(await fs.readFile(projectedConfig, "utf8"), "model = \"test\"\n");
  if (process.platform !== "win32") {
    assert.equal((await fs.stat(path.join(policy.agentHome, ".codex"))).mode & 0o777, 0o700);
    assert.equal((await fs.stat(projectedAuth)).mode & 0o777, 0o600);
  }
});

test("PolicyResolver preview avoids credential projection side effects", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-policy-preview-"));
  const sourceHome = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-source-home-"));
  await fs.mkdir(path.join(sourceHome, ".codex"), { recursive: true });
  await fs.writeFile(path.join(sourceHome, ".codex", "auth.json"), "{\"token\":\"test\"}\n");

  const store = new StateStore(root);
  await store.init();
  const agent = await store.createAgent({
    name: "Codex Preview Agent",
    provider: "codex",
    accessMode: "safe",
  });

  const policy = await new PolicyResolver(store, { credentialSourceHome: sourceHome }).preview(agent);

  assert.equal(policy.env.CODEX_HOME, path.join(policy.agentHome, ".codex"));
  await assert.rejects(fs.access(policy.agentHome), /ENOENT/);
  await assert.rejects(fs.access(path.join(policy.agentHome, ".codex", "auth.json")), /ENOENT/);
});

test("PolicyResolver does not project provider credentials in trusted mode", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-policy-trusted-"));
  const sourceHome = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-source-home-"));
  await fs.mkdir(path.join(sourceHome, ".codex"), { recursive: true });
  await fs.writeFile(path.join(sourceHome, ".codex", "auth.json"), "{\"token\":\"test\"}\n");

  const store = new StateStore(root);
  await store.init();
  const agent = await store.createAgent({
    name: "Trusted Codex",
    provider: "codex",
    accessMode: "trusted",
  });

  const policy = await new PolicyResolver(store, { credentialSourceHome: sourceHome }).resolve(agent);
  assert.equal(policy.env.HOME, process.env.HOME);
  assert.notEqual(policy.env.CODEX_HOME, path.join(policy.agentHome, ".codex"));
  await assert.rejects(fs.access(path.join(policy.agentHome, ".codex", "auth.json")), /ENOENT/);
});

test("PolicyResolver projects explicit provider config paths into isolated home", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-policy-provider-config-"));
  const sourceHome = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-source-home-"));
  await fs.mkdir(path.join(sourceHome, ".claude", "commands"), { recursive: true });
  await fs.writeFile(path.join(sourceHome, ".claude", "commands", "build.md"), "/build\n");
  await fs.writeFile(path.join(sourceHome, ".claude", "settings.local.json"), "{\"mcpServers\":{}}\n");

  const store = new StateStore(root);
  await store.init();
  const agent = await store.createAgent({
    name: "Claude Config Agent",
    provider: "claude",
    accessMode: "safe",
    providerConfigPaths: [".claude/commands", ".claude/settings.local.json"],
  });

  const policy = await new PolicyResolver(store, { credentialSourceHome: sourceHome }).resolve(agent);

  assert.equal(await fs.readFile(path.join(policy.agentHome, ".claude", "commands", "build.md"), "utf8"), "/build\n");
  assert.equal(
    await fs.readFile(path.join(policy.agentHome, ".claude", "settings.local.json"), "utf8"),
    "{\"mcpServers\":{}}\n",
  );
  if (process.platform !== "win32") {
    assert.equal((await fs.stat(path.join(policy.agentHome, ".claude", "commands"))).mode & 0o777, 0o700);
    assert.equal((await fs.stat(path.join(policy.agentHome, ".claude", "commands", "build.md"))).mode & 0o777, 0o600);
  }
});

test("PolicyResolver does not project symlinks from explicit provider config paths", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-policy-provider-config-symlink-"));
  const sourceHome = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-source-home-"));
  const sensitive = path.join(sourceHome, "sensitive.txt");
  await fs.mkdir(path.join(sourceHome, ".claude", "commands"), { recursive: true });
  await fs.writeFile(sensitive, "do-not-copy\n");
  await fs.writeFile(path.join(sourceHome, ".claude", "commands", "safe.md"), "safe\n");
  await fs.symlink(sensitive, path.join(sourceHome, ".claude", "commands", "secret.md"));

  const store = new StateStore(root);
  await store.init();
  const agent = await store.createAgent({
    name: "Claude Config Symlink Agent",
    provider: "claude",
    accessMode: "safe",
    providerConfigPaths: [".claude/commands"],
  });

  const policy = await new PolicyResolver(store, { credentialSourceHome: sourceHome }).resolve(agent);

  assert.equal(await fs.readFile(path.join(policy.agentHome, ".claude", "commands", "safe.md"), "utf8"), "safe\n");
  await assert.rejects(fs.access(path.join(policy.agentHome, ".claude", "commands", "secret.md")), /ENOENT/);
});

test("PolicyResolver ignores non-provider config paths from persisted state", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-policy-provider-config-filter-"));
  const sourceHome = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-source-home-"));
  await fs.mkdir(path.join(sourceHome, ".ssh"), { recursive: true });
  await fs.mkdir(path.join(sourceHome, ".codex", "skills"), { recursive: true });
  await fs.writeFile(path.join(sourceHome, ".ssh", "config"), "do-not-copy\n");
  await fs.writeFile(path.join(sourceHome, ".codex", "history.jsonl"), "do-not-copy\n");
  await fs.writeFile(path.join(sourceHome, ".codex", "skills", "safe.md"), "safe\n");

  const store = new StateStore(root);
  await store.init();
  const agent = await store.createAgent({
    name: "Codex Config Filter Agent",
    provider: "codex",
    accessMode: "safe",
    providerConfigPaths: [".codex/skills"],
  });
  agent.providerConfigPaths = [".codex/skills", ".ssh/config", ".codex", ".codex/history.jsonl"];

  const policy = await new PolicyResolver(store, { credentialSourceHome: sourceHome }).resolve(agent);

  assert.equal(await fs.readFile(path.join(policy.agentHome, ".codex", "skills", "safe.md"), "utf8"), "safe\n");
  await assert.rejects(fs.access(path.join(policy.agentHome, ".ssh", "config")), /ENOENT/);
  await assert.rejects(fs.access(path.join(policy.agentHome, ".codex", "history.jsonl")), /ENOENT/);
});

test("PolicyResolver skips oversized provider config projection files", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-policy-provider-config-size-"));
  const sourceHome = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-source-home-"));
  await fs.mkdir(path.join(sourceHome, ".codex", "skills"), { recursive: true });
  await fs.writeFile(path.join(sourceHome, ".codex", "skills", "small.md"), "small\n");
  await fs.writeFile(path.join(sourceHome, ".codex", "skills", "large.md"), Buffer.alloc(2 * 1024 * 1024 + 1, "x"));

  const store = new StateStore(root);
  await store.init();
  const agent = await store.createAgent({
    name: "Codex Config Size Agent",
    provider: "codex",
    accessMode: "safe",
    providerConfigPaths: [".codex/skills"],
  });

  const policy = await new PolicyResolver(store, { credentialSourceHome: sourceHome }).resolve(agent);

  assert.equal(await fs.readFile(path.join(policy.agentHome, ".codex", "skills", "small.md"), "utf8"), "small\n");
  await assert.rejects(fs.access(path.join(policy.agentHome, ".codex", "skills", "large.md")), /ENOENT/);
});

test("PolicyResolver strips daemon-internal env from trusted provider processes", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-policy-trusted-env-"));
  const previousAuth = process.env.AGENT_CORE_AUTH_TOKEN;
  const previousProfile = process.env.AGENT_CORE_NATIVE_PROFILE;
  const previousOpenAi = process.env.OPENAI_API_KEY;
  process.env.AGENT_CORE_AUTH_TOKEN = "server-token";
  process.env.AGENT_CORE_NATIVE_PROFILE = "prod";
  process.env.OPENAI_API_KEY = "user-provider-token";

  try {
    const store = new StateStore(root);
    await store.init();
    const agent = await store.createAgent({
      name: "Trusted Codex",
      provider: "codex",
      accessMode: "trusted",
    });

    const policy = await new PolicyResolver(store).resolve(agent);
    assert.equal(policy.env.AGENT_CORE_AUTH_TOKEN, undefined);
    assert.equal(policy.env.AGENT_CORE_NATIVE_PROFILE, undefined);
    assert.equal(policy.env.AGENT_CORE_HOME, undefined);
    assertNoAgentCoreEnv(policy.env);
    assert.equal(policy.env.AGENT_ID, agent.id);
    assert.equal(policy.env.OPENAI_API_KEY, "user-provider-token");
  } finally {
    restoreEnv("AGENT_CORE_AUTH_TOKEN", previousAuth);
    restoreEnv("AGENT_CORE_NATIVE_PROFILE", previousProfile);
    restoreEnv("OPENAI_API_KEY", previousOpenAi);
  }
});

test("PolicyResolver gives safe and project providers a minimal env", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-policy-minimal-env-"));
  const project = path.join(root, "project");
  await fs.mkdir(project);
  const previousAuth = process.env.AGENT_CORE_AUTH_TOKEN;
  const previousOpenAi = process.env.OPENAI_API_KEY;
  const previousAnthropic = process.env.ANTHROPIC_API_KEY;
  process.env.AGENT_CORE_AUTH_TOKEN = "server-token";
  process.env.OPENAI_API_KEY = "openai-token";
  process.env.ANTHROPIC_API_KEY = "anthropic-token";

  try {
    const store = new StateStore(root);
    await store.init();
    const safeAgent = await store.createAgent({
      name: "Safe Codex",
      provider: "codex",
      accessMode: "safe",
    });
    const projectAgent = await store.createAgent({
      name: "Project Codex",
      provider: "codex",
      accessMode: "project",
      projectPath: project,
    });

    const safePolicy = await new PolicyResolver(store, { projectProviderCredentials: false }).resolve(safeAgent);
    const projectPolicy = await new PolicyResolver(store, { projectProviderCredentials: false }).resolve(projectAgent);
    for (const policy of [safePolicy, projectPolicy]) {
      assert.equal(policy.env.AGENT_CORE_AUTH_TOKEN, undefined);
      assert.equal(policy.env.OPENAI_API_KEY, undefined);
      assert.equal(policy.env.ANTHROPIC_API_KEY, undefined);
      assert.equal(policy.env.AGENT_CORE_HOME, undefined);
      assertNoAgentCoreEnv(policy.env);
      assert.equal(typeof policy.env.AGENT_ID, "string");
      assert.equal(policy.env.HOME, policy.agentHome);
      assert.equal(policy.env.USERPROFILE, policy.agentHome);
      assert.equal(policy.env.PATH, process.env.PATH);
    }
  } finally {
    restoreEnv("AGENT_CORE_AUTH_TOKEN", previousAuth);
    restoreEnv("OPENAI_API_KEY", previousOpenAi);
    restoreEnv("ANTHROPIC_API_KEY", previousAnthropic);
  }
});

test("PolicyResolver honors global credential projection opt-out", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-policy-credentials-off-"));
  const sourceHome = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-source-home-"));
  await fs.mkdir(path.join(sourceHome, ".codex"), { recursive: true });
  await fs.writeFile(path.join(sourceHome, ".codex", "auth.json"), "{\"token\":\"test\"}\n");
  const previous = process.env.AGENT_CORE_CREDENTIALS;
  process.env.AGENT_CORE_CREDENTIALS = "off";

  try {
    const store = new StateStore(root);
    await store.init();
    const agent = await store.createAgent({
      name: "Codex Agent",
      provider: "codex",
      accessMode: "safe",
    });

    const policy = await new PolicyResolver(store, { credentialSourceHome: sourceHome }).resolve(agent);

    assert.equal(policy.env.CODEX_HOME, path.join(policy.agentHome, ".codex"));
    await assert.rejects(fs.access(path.join(policy.agentHome, ".codex", "auth.json")), /ENOENT/);
  } finally {
    restoreEnv("AGENT_CORE_CREDENTIALS", previous);
  }
});

test("PolicyResolver does not project symlinked provider credentials", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-policy-symlink-"));
  const sourceHome = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-source-home-"));
  await fs.mkdir(path.join(sourceHome, ".codex"), { recursive: true });
  const sensitive = path.join(sourceHome, "sensitive.txt");
  await fs.writeFile(sensitive, "do-not-copy\n");
  await fs.symlink(sensitive, path.join(sourceHome, ".codex", "auth.json"));

  const store = new StateStore(root);
  await store.init();
  const agent = await store.createAgent({
    name: "Codex Agent",
    provider: "codex",
    accessMode: "safe",
  });

  const policy = await new PolicyResolver(store, { credentialSourceHome: sourceHome }).resolve(agent);
  await assert.rejects(fs.access(path.join(policy.agentHome, ".codex", "auth.json")), /ENOENT/);
});

test("PolicyResolver does not project provider credentials through symlinked source directories", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-policy-source-dir-symlink-"));
  const sourceHome = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-source-home-"));
  const outside = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-source-outside-"));
  await fs.writeFile(path.join(outside, "auth.json"), "{\"token\":\"test\"}\n");
  await fs.symlink(outside, path.join(sourceHome, ".codex"));

  const store = new StateStore(root);
  await store.init();
  const agent = await store.createAgent({
    name: "Codex Agent",
    provider: "codex",
    accessMode: "safe",
  });

  const policy = await new PolicyResolver(store, { credentialSourceHome: sourceHome }).resolve(agent);

  await assert.rejects(fs.access(path.join(policy.agentHome, ".codex", "auth.json")), /ENOENT/);
});

test("PolicyResolver does not project provider credentials through symlinked source homes", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-policy-source-home-symlink-"));
  const realSourceHome = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-source-real-home-"));
  const sourceHome = path.join(root, "source-home-link");
  await fs.mkdir(path.join(realSourceHome, ".codex"), { recursive: true });
  await fs.writeFile(path.join(realSourceHome, ".codex", "auth.json"), "{\"token\":\"test\"}\n");
  await fs.symlink(realSourceHome, sourceHome);

  const store = new StateStore(root);
  await store.init();
  const agent = await store.createAgent({
    name: "Codex Agent",
    provider: "codex",
    accessMode: "safe",
  });

  const policy = await new PolicyResolver(store, { credentialSourceHome: sourceHome }).resolve(agent);

  await assert.rejects(fs.access(path.join(policy.agentHome, ".codex", "auth.json")), /ENOENT/);
});

test("PolicyResolver does not project provider credentials through symlinked target directories", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-policy-target-symlink-"));
  const sourceHome = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-source-home-"));
  const outside = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-outside-"));
  await fs.mkdir(path.join(sourceHome, ".codex"), { recursive: true });
  await fs.writeFile(path.join(sourceHome, ".codex", "auth.json"), "{\"token\":\"test\"}\n");

  const store = new StateStore(root);
  await store.init();
  const agent = await store.createAgent({
    name: "Codex Agent",
    provider: "codex",
    accessMode: "safe",
  });
  const agentHome = path.join(root, "agents", agent.id, "home");
  await fs.mkdir(agentHome, { recursive: true });
  await fs.symlink(outside, path.join(agentHome, ".codex"));

  const policy = await new PolicyResolver(store, { credentialSourceHome: sourceHome }).resolve(agent);

  await assert.rejects(fs.access(path.join(outside, "auth.json")), /ENOENT/);
  await assert.rejects(fs.access(path.join(policy.agentHome, ".codex", "auth.json")), /ENOENT/);
});

test("PolicyResolver replaces symlinked target credential files safely", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-policy-target-file-symlink-"));
  const sourceHome = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-source-home-"));
  const outside = path.join(root, "outside-auth.json");
  await fs.mkdir(path.join(sourceHome, ".codex"), { recursive: true });
  await fs.writeFile(path.join(sourceHome, ".codex", "auth.json"), "{\"token\":\"test\"}\n");
  await fs.writeFile(outside, "do-not-overwrite\n");

  const store = new StateStore(root);
  await store.init();
  const agent = await store.createAgent({
    name: "Codex Agent",
    provider: "codex",
    accessMode: "safe",
  });
  const targetDir = path.join(root, "agents", agent.id, "home", ".codex");
  await fs.mkdir(targetDir, { recursive: true });
  await fs.symlink(outside, path.join(targetDir, "auth.json"));

  const policy = await new PolicyResolver(store, { credentialSourceHome: sourceHome }).resolve(agent);

  assert.equal(await fs.readFile(outside, "utf8"), "do-not-overwrite\n");
  assert.equal(await fs.readFile(path.join(policy.agentHome, ".codex", "auth.json"), "utf8"), "{\"token\":\"test\"}\n");
  assert.equal((await fs.lstat(path.join(policy.agentHome, ".codex", "auth.json"))).isSymbolicLink(), false);
});

test("PolicyResolver rejects symlinked isolated agent home roots", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-policy-home-symlink-"));
  const sourceHome = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-source-home-"));
  const outside = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-outside-home-"));
  await fs.mkdir(path.join(sourceHome, ".codex"), { recursive: true });
  await fs.writeFile(path.join(sourceHome, ".codex", "auth.json"), "{\"token\":\"test\"}\n");

  const store = new StateStore(root);
  await store.init();
  const agent = await store.createAgent({
    name: "Codex Agent",
    provider: "codex",
    accessMode: "safe",
  });
  await fs.symlink(outside, path.join(root, "agents", agent.id, "home"));

  await assert.rejects(
    new PolicyResolver(store, { credentialSourceHome: sourceHome }).resolve(agent),
    /unsafe filesystem path/,
  );
  await assert.rejects(
    new PolicyResolver(store, { credentialSourceHome: sourceHome }).preview(agent),
    /unsafe filesystem path/,
  );
  await assert.rejects(fs.access(path.join(outside, ".codex", "auth.json")), /ENOENT/);
});

test("PolicyResolver rejects symlinked isolated workspace roots", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-policy-workspace-symlink-"));
  const outside = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-outside-workspace-"));

  const store = new StateStore(root);
  await store.init();
  const agent = await store.createAgent({
    name: "Codex Agent",
    provider: "codex",
    accessMode: "safe",
  });
  await fs.rm(agent.workspace, { recursive: true, force: true });
  await fs.symlink(outside, agent.workspace);

  await assert.rejects(
    new PolicyResolver(store, { projectProviderCredentials: false }).resolve(agent),
    /unsafe filesystem path/,
  );
  await assert.rejects(
    new PolicyResolver(store, { projectProviderCredentials: false }).preview(agent),
    /unsafe filesystem path/,
  );
});

test("PolicyResolver canonicalizes persisted project paths", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-policy-project-canonical-"));
  const project = path.join(root, "project");
  const link = path.join(root, "project-link");
  await fs.mkdir(project);
  await fs.symlink(project, link);
  const store = new StateStore(root);
  await store.init();
  const agent = await store.createAgent({
    name: "Project Agent",
    provider: "codex",
    accessMode: "project",
    projectPath: link,
  });

  const policy = await new PolicyResolver(store, { projectProviderCredentials: false }).resolve(agent);

  assert.equal(policy.cwd, await fs.realpath(project));
  assert.equal(policy.projectPath, await fs.realpath(project));
});

test("PolicyResolver rejects invalid persisted project paths", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-policy-project-invalid-"));
  const store = new StateStore(root);
  await store.init();
  const missing = await store.createAgent({
    name: "Missing Project Agent",
    provider: "codex",
    accessMode: "project",
  });
  const relative = await store.createAgent({
    name: "Relative Project Agent",
    provider: "codex",
    accessMode: "project",
    projectPath: "relative",
  });

  await assert.rejects(
    new PolicyResolver(store, { projectProviderCredentials: false }).resolve(missing),
    /projectPath is required/,
  );
  await assert.rejects(
    new PolicyResolver(store, { projectProviderCredentials: false }).resolve(relative),
    /projectPath must be absolute/,
  );
});

test("PolicyResolver honors global network deny default", { skip: process.platform !== "darwin" }, async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-policy-network-off-"));
  const previousNetwork = process.env.AGENT_CORE_NETWORK;
  const previousSandbox = process.env.AGENT_CORE_SANDBOX;
  process.env.AGENT_CORE_NETWORK = "off";
  delete process.env.AGENT_CORE_SANDBOX;

  const store = new StateStore(root);
  await store.init();
  const agent = await store.createAgent({
    name: "Network Off Agent",
    provider: "codex",
    accessMode: "safe",
  });

  try {
    const policy = await new PolicyResolver(store, { projectProviderCredentials: false }).resolve(agent);

    assert.equal(policy.networkAccess, "deny");
    assert.equal(policy.sandbox?.network, "deny");
    const profile = buildDarwinSandboxProfile(policy.sandbox!);
    assert.doesNotMatch(profile, /\(allow network\*\)/);
  } finally {
    restoreEnv("AGENT_CORE_NETWORK", previousNetwork);
    restoreEnv("AGENT_CORE_SANDBOX", previousSandbox);
  }
});

test("PolicyResolver maps per-agent network deny into macOS sandbox", { skip: process.platform !== "darwin" }, async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-policy-network-"));
  const store = new StateStore(root);
  await store.init();
  const agent = await store.createAgent({
    name: "Network Deny Agent",
    provider: "codex",
    accessMode: "safe",
    networkAccess: "deny",
    deniedTools: ["python"],
  });

  const previous = process.env.AGENT_CORE_SANDBOX;
  process.env.AGENT_CORE_SANDBOX = "1";
  try {
    const policy = await new PolicyResolver(store).resolve(agent);
    assert.equal(policy.networkAccess, "deny");
    assert.deepEqual(policy.deniedTools, ["python"]);
    assert.equal(policy.sandbox?.network, "deny");
    const profile = buildDarwinSandboxProfile(policy.sandbox!);
    assert.doesNotMatch(profile, /\(allow network\*\)/);
    assert.match(profile, /\(literal "\/usr\/bin\/python"\)/);
  } finally {
    if (previous === undefined) delete process.env.AGENT_CORE_SANDBOX;
    else process.env.AGENT_CORE_SANDBOX = previous;
  }
});

test("PolicyResolver auto-enforces restrictive policies with macOS sandbox", { skip: process.platform !== "darwin" }, async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-policy-auto-sandbox-"));
  const store = new StateStore(root);
  await store.init();
  const agent = await store.createAgent({
    name: "Auto Sandbox Agent",
    provider: "codex",
    accessMode: "safe",
    networkAccess: "deny",
  });

  const previous = process.env.AGENT_CORE_SANDBOX;
  delete process.env.AGENT_CORE_SANDBOX;
  try {
    const policy = await new PolicyResolver(store, { projectProviderCredentials: false }).resolve(agent);
    assert.equal(policy.sandbox?.enabled, true);
    assert.equal(policy.sandbox?.network, "deny");
  } finally {
    if (previous === undefined) delete process.env.AGENT_CORE_SANDBOX;
    else process.env.AGENT_CORE_SANDBOX = previous;
  }
});

test("PolicyResolver sandboxes safe agents by default on macOS", { skip: process.platform !== "darwin" }, async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-policy-default-sandbox-"));
  const store = new StateStore(root);
  await store.init();
  const agent = await store.createAgent({
    name: "Plain Agent",
    provider: "codex",
    accessMode: "safe",
  });

  const previous = process.env.AGENT_CORE_SANDBOX;
  delete process.env.AGENT_CORE_SANDBOX;
  try {
    const policy = await new PolicyResolver(store, { projectProviderCredentials: false }).resolve(agent);
    assert.equal(policy.sandbox?.enabled, true);
    assert.equal(policy.sandbox?.network, "inherit");
    assert.deepEqual(
      policy.sandbox?.writableRoots.sort(),
      [policy.agentHome, policy.workspace, path.resolve(process.env.TMPDIR || "/tmp")].sort(),
    );
  } finally {
    if (previous === undefined) delete process.env.AGENT_CORE_SANDBOX;
    else process.env.AGENT_CORE_SANDBOX = previous;
  }
});

test("PolicyResolver maps file access policy into macOS sandbox roots", { skip: process.platform !== "darwin" }, async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-policy-file-access-"));
  const readable = path.join(root, "readable");
  const writable = path.join(root, "writable");
  const writableLink = path.join(root, "writable-link");
  await fs.mkdir(readable);
  await fs.mkdir(writable);
  await fs.symlink(writable, writableLink);
  const store = new StateStore(root);
  await store.init();
  const agent = await store.createAgent({
    name: "File Access Agent",
    provider: "codex",
    accessMode: "safe",
    fileAccess: {
      readablePaths: [readable],
      writablePaths: [writableLink],
    },
  });

  const previous = process.env.AGENT_CORE_SANDBOX;
  delete process.env.AGENT_CORE_SANDBOX;
  try {
    const policy = await new PolicyResolver(store, { projectProviderCredentials: false }).resolve(agent);

    assert.deepEqual(policy.fileAccess, {
      readablePaths: [await fs.realpath(readable)],
      writablePaths: [await fs.realpath(writable)],
    });
    assert.ok(policy.sandbox?.readableRoots.includes(await fs.realpath(readable)));
    assert.ok(policy.sandbox?.readableRoots.includes(await fs.realpath(writable)));
    assert.ok(policy.sandbox?.writableRoots.includes(await fs.realpath(writable)));
    assert.equal(policy.sandbox?.writableRoots.includes(await fs.realpath(readable)), false);
  } finally {
    if (previous === undefined) delete process.env.AGENT_CORE_SANDBOX;
    else process.env.AGENT_CORE_SANDBOX = previous;
  }
});

test("PolicyResolver rejects invalid persisted file access paths", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-policy-file-access-invalid-"));
  const store = new StateStore(root);
  await store.init();
  const relative = await store.createAgent({
    name: "Relative File Access Agent",
    provider: "codex",
    accessMode: "safe",
    fileAccess: { readablePaths: ["relative"] },
  });
  const missing = await store.createAgent({
    name: "Missing File Access Agent",
    provider: "codex",
    accessMode: "safe",
    fileAccess: { writablePaths: [path.join(root, "missing")] },
  });

  await assert.rejects(
    new PolicyResolver(store, { projectProviderCredentials: false }).resolve(relative),
    /fileAccess\.readablePaths entries must be absolute paths/,
  );
  await assert.rejects(
    new PolicyResolver(store, { projectProviderCredentials: false }).preview(missing),
    /fileAccess\.writablePaths entries must be existing directories/,
  );
});

test("PolicyResolver does not grant broad PATH roots to safe sandboxes", { skip: process.platform !== "darwin" }, async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-policy-path-roots-"));
  const toolDir = path.join(root, "tools", "bin");
  await fs.mkdir(toolDir, { recursive: true });
  const store = new StateStore(root);
  await store.init();
  const agent = await store.createAgent({
    name: "Path Agent",
    provider: "codex",
    accessMode: "safe",
  });
  const previousPath = process.env.PATH;
  const previousSandbox = process.env.AGENT_CORE_SANDBOX;
  process.env.PATH = [
    "/",
    ".",
    os.homedir(),
    path.dirname(os.homedir()),
    toolDir,
    "/usr/bin",
  ].join(path.delimiter);
  delete process.env.AGENT_CORE_SANDBOX;

  try {
    const policy = await new PolicyResolver(store, { projectProviderCredentials: false }).resolve(agent);
    const readableRoots = new Set(policy.sandbox?.readableRoots);

    assert.equal(readableRoots.has(path.resolve("/")), false);
    assert.equal(readableRoots.has(path.resolve(".")), false);
    assert.equal(readableRoots.has(path.resolve(os.homedir())), false);
    assert.equal(readableRoots.has(path.resolve(path.dirname(os.homedir()))), false);
    assert.equal(readableRoots.has(path.resolve(toolDir)), true);
    assert.equal(readableRoots.has(path.resolve("/usr/bin")), true);
  } finally {
    restoreEnv("PATH", previousPath);
    restoreEnv("AGENT_CORE_SANDBOX", previousSandbox);
  }
});

test("PolicyResolver allows unrestricted sandbox opt-out for local debugging", { skip: process.platform !== "darwin" }, async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-policy-sandbox-off-"));
  const store = new StateStore(root);
  await store.init();
  const agent = await store.createAgent({
    name: "Debug Agent",
    provider: "codex",
    accessMode: "safe",
  });

  const previous = process.env.AGENT_CORE_SANDBOX;
  process.env.AGENT_CORE_SANDBOX = "off";
  try {
    const policy = await new PolicyResolver(store, { projectProviderCredentials: false }).resolve(agent);
    assert.equal(policy.sandbox, undefined);
  } finally {
    if (previous === undefined) delete process.env.AGENT_CORE_SANDBOX;
    else process.env.AGENT_CORE_SANDBOX = previous;
  }
});

test("PolicyResolver keeps restrictive policies sandboxed even when opt-out is set", { skip: process.platform !== "darwin" }, async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "agent-core-policy-sandbox-off-restrictive-"));
  const store = new StateStore(root);
  await store.init();
  const agent = await store.createAgent({
    name: "Restricted Debug Agent",
    provider: "codex",
    accessMode: "safe",
    networkAccess: "deny",
  });

  const previous = process.env.AGENT_CORE_SANDBOX;
  process.env.AGENT_CORE_SANDBOX = "off";
  try {
    const policy = await new PolicyResolver(store, { projectProviderCredentials: false }).resolve(agent);
    assert.equal(policy.sandbox?.enabled, true);
    assert.equal(policy.sandbox?.network, "deny");
  } finally {
    if (previous === undefined) delete process.env.AGENT_CORE_SANDBOX;
    else process.env.AGENT_CORE_SANDBOX = previous;
  }
});

function restoreEnv(name: string, value: string | undefined): void {
  if (value === undefined) delete process.env[name];
  else process.env[name] = value;
}

function assertNoAgentCoreEnv(env: NodeJS.ProcessEnv): void {
  assert.deepEqual(Object.keys(env).filter((key) => key.startsWith("AGENT_CORE_")), []);
}
