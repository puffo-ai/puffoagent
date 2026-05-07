import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { promisify } from "node:util";
import test from "node:test";

const execFileAsync = promisify(execFile);

async function makeTempDir(prefix: string): Promise<string> {
  const tempRoot = await fs.realpath(os.tmpdir());
  return fs.mkdtemp(path.join(tempRoot, prefix));
}

test("package metadata exposes a publish-valid agent binary", async () => {
  const packageJson = JSON.parse(await fs.readFile("package.json", "utf8"));
  const readme = await fs.readFile("README.md", "utf8");

  assert.equal(packageJson.name, "@puffo-ai/agent-core");
  assert.equal(packageJson.bin?.agent, "dist/src/cli/index.js");
  assert.equal(packageJson.publishConfig?.access, "public");
  assert.equal(packageJson.publishConfig?.registry, "https://registry.npmjs.org/");
  assert.equal(
    packageJson.scripts?.prepublishOnly,
    "node scripts/assert-release-profile.mjs && npm run check:package",
  );
  assert.equal(packageJson.scripts?.["check:package"], "node scripts/check-pack-manifest.mjs");
  assert.equal(packageJson.scripts?.["check:core-patch"], "node scripts/check-core-upstream-patch.mjs");
  assert.equal(packageJson.scripts?.["export:core-patch"], "node scripts/export-core-upstream-patch.mjs");
  assert.deepEqual(packageJson.files, ["bin/", "dist/src/", "README.md"]);
  assert.equal(packageJson.files.includes("scripts/"), false);
  assert.doesNotMatch(readme, /\]\(\.\.\/docs\//, "README.md is published without repository docs/");
});

test("gitignore excludes generated package artifacts without hiding Rust bin sources", async () => {
  const gitignore = (await fs.readFile(".gitignore", "utf8"))
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);

  assert.ok(gitignore.includes("/bin/"));
  assert.equal(gitignore.includes("bin/"), false);
  for (const pattern of ["dist/", "node_modules/", "target/", "*.tgz", "*.sqlite", ".env", ".env.*"]) {
    assert.ok(gitignore.includes(pattern), `missing .gitignore pattern: ${pattern}`);
  }

  await assertGitIgnored("bin/darwin/arm64/agent-native-sidecar");
  await assertGitIgnored("dist/src/cli/index.js");
  await assertGitIgnored("node_modules/typescript/package.json");
  await assertGitIgnored("crates/agent-native/target/release/agent-native-sidecar");
  await assertGitIgnored("puffo-ai-agent-core-0.1.0.tgz");
  await assertGitIgnored("core.sqlite");
  await assertGitIgnored(".env.local");
  await assertGitNotIgnored("crates/agent-native/src/bin/agent-native-sidecar.rs");
});

test("root gitignore excludes local reference checkouts without hiding submodules", async () => {
  const gitignore = (await fs.readFile(path.join("..", ".gitignore"), "utf8"))
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);

  assert.ok(gitignore.includes("/core/"));
  assert.ok(gitignore.includes("/hermes-agent/"));
  assert.ok(gitignore.includes(".DS_Store"));
  assert.equal(gitignore.includes("/agent-core/core/"), false);
  assert.equal(gitignore.includes("/puffo-core-han-group/"), false);

  await assertRootGitIgnored("core/README.md");
  await assertRootGitIgnored("hermes-agent/README.md");
  await assertRootGitIgnored(".DS_Store");
  await assertRootGitNotIgnored("agent-core/core/README.md");
  await assertRootGitNotIgnored("puffo-core-han-group/README.md");
});

test("root gitmodules records tracked submodules with HTTPS clone URLs", async () => {
  const gitmodules = await fs.readFile(path.join("..", ".gitmodules"), "utf8");

  assert.match(gitmodules, /\[submodule "agent-core\/core"\]/);
  assert.match(gitmodules, /path = agent-core\/core/);
  assert.match(gitmodules, /url = https:\/\/github\.com\/puffo-ai\/core\.git/);
  assert.match(gitmodules, /\[submodule "puffo-core-han-group"\]/);
  assert.match(gitmodules, /path = puffo-core-han-group/);
  assert.match(gitmodules, /url = https:\/\/github\.com\/puffo-ai\/puffo-core-han-group\.git/);
  assert.doesNotMatch(gitmodules, /git@github\.com:/);
});

test("root gitlink records the checked-out core submodule revision", async () => {
  const root = path.resolve("..");
  const recorded = await execFileAsync("git", ["ls-files", "-s", "agent-core/core"], { cwd: root });
  const actual = await execFileAsync("git", ["-C", "agent-core/core", "rev-parse", "HEAD"], {
    cwd: root,
  });
  const match = recorded.stdout.match(/^160000 ([0-9a-f]{40}) 0\tagent-core\/core$/m);

  assert.ok(match, "agent-core/core must be recorded as a git submodule");
  assert.equal(match[1], actual.stdout.trim());
});

test("core upstream patch records an explicit base revision", async () => {
  const base = await fs.readFile(path.join("..", "docs", "patches", "agent-core-core-upstream.base"), "utf8");

  assert.match(base.trim(), /^[0-9a-f]{40}$/);
});

test("prepublish release gate requires the production native build profile", async () => {
  const script = path.join("scripts", "assert-release-profile.mjs");
  const defaultEnv = { ...process.env };
  delete defaultEnv.AGENT_CORE_NATIVE_BUILD_PROFILE;

  await assert.rejects(
    execFileAsync(process.execPath, [script], { env: defaultEnv }),
    (error: unknown) => {
      const failed = error as { code?: number; stderr?: string };
      assert.equal(failed.code, 1);
      assert.match(failed.stderr ?? "", /Refusing to publish/);
      return true;
    },
  );

  const prod = await execFileAsync(process.execPath, [script], {
    env: { ...defaultEnv, AGENT_CORE_NATIVE_BUILD_PROFILE: "prod" },
  });
  assert.equal(prod.stdout, "");
  assert.equal(prod.stderr, "");
});

test("prepublish lifecycle refuses dev profile before package manifest check", async () => {
  const tempDir = await makeTempDir("agent-core-prepublish-dev-");
  const marker = path.join(tempDir, "check-package-ran");
  const npm = await resolveNpmCommand();

  try {
    await writeFakeNpm(tempDir, marker);
    const env: NodeJS.ProcessEnv = {
      ...process.env,
      PATH: `${tempDir}${path.delimiter}${process.env.PATH ?? ""}`,
      AGENT_CORE_TEST_MARKER: marker,
    };
    delete env.AGENT_CORE_NATIVE_BUILD_PROFILE;

    await assert.rejects(
      execFileAsync(npm.command, [...npm.args, "run", "prepublishOnly"], { env }),
      (error: unknown) => {
        const failed = error as { code?: number; stderr?: string };
        assert.equal(failed.code, 1);
        assert.match(failed.stderr ?? "", /Refusing to publish/);
        return true;
      },
    );
    assert.equal(await fileExists(marker), false);
  } finally {
    await fs.rm(tempDir, { recursive: true, force: true });
  }
});

test("prepublish lifecycle runs package manifest check for prod profile", async () => {
  const tempDir = await makeTempDir("agent-core-prepublish-prod-");
  const marker = path.join(tempDir, "check-package-ran");
  const npm = await resolveNpmCommand();

  try {
    await writeFakeNpm(tempDir, marker);
    const result = await execFileAsync(npm.command, [...npm.args, "run", "prepublishOnly"], {
      env: {
        ...process.env,
        PATH: `${tempDir}${path.delimiter}${process.env.PATH ?? ""}`,
        AGENT_CORE_NATIVE_BUILD_PROFILE: "prod",
        AGENT_CORE_TEST_MARKER: marker,
      },
    });

    assert.match(result.stdout, /fake check:package ok/);
    assert.equal((await fs.readFile(marker, "utf8")).trim(), "ran");
    assert.equal(result.stderr, "");
  } finally {
    await fs.rm(tempDir, { recursive: true, force: true });
  }
});

test("core patch scripts explain missing submodule setup", async () => {
  const tempDir = await makeTempDir("agent-core-core-patch-missing-");
  const fakePackageRoot = path.join(tempDir, "agent-core");
  const patchDir = path.join(tempDir, "docs", "patches");
  const expected = /git submodule update --init agent-core\/core/;

  try {
    await fs.mkdir(fakePackageRoot, { recursive: true });
    await fs.mkdir(patchDir, { recursive: true });
    await fs.writeFile(path.join(patchDir, "agent-core-core-upstream.patch"), "diff --git a/a b/a\n");

    await assert.rejects(
      execFileAsync(process.execPath, [path.resolve("scripts", "check-core-upstream-patch.mjs")], {
        cwd: fakePackageRoot,
      }),
      (error: unknown) => {
        const failed = error as { code?: number; stderr?: string };
        assert.equal(failed.code, 1);
        assert.match(failed.stderr ?? "", expected);
        return true;
      },
    );

    await assert.rejects(
      execFileAsync(process.execPath, [path.resolve("scripts", "export-core-upstream-patch.mjs")], {
        cwd: fakePackageRoot,
      }),
      (error: unknown) => {
        const failed = error as { code?: number; stderr?: string };
        assert.equal(failed.code, 1);
        assert.match(failed.stderr ?? "", expected);
        return true;
      },
    );
  } finally {
    await fs.rm(tempDir, { recursive: true, force: true });
  }
});

test("macOS bootstrap script is syntactically valid", async () => {
  const result = await execFileAsync("bash", ["-n", path.join("scripts", "bootstrap-macos.sh")]);
  assert.equal(result.stdout, "");
  assert.equal(result.stderr, "");
});

test("macOS bootstrap validates requested Node major before download", async () => {
  const tempDir = await makeTempDir("agent-core-bootstrap-node-major-");
  const pathBin = path.join(tempDir, "path-bin");
  const home = path.join(tempDir, "home");

  try {
    await fs.mkdir(pathBin, { recursive: true });
    await fs.writeFile(
      path.join(pathBin, "uname"),
      "#!/usr/bin/env bash\nif [ \"${1:-}\" = \"-m\" ]; then printf 'arm64\\n'; else printf 'Darwin\\n'; fi\n",
      { mode: 0o755 },
    );

    for (const [major, expected] of [
      ["lts", /AGENT_CORE_NODE_MAJOR must be an integer/],
      ["18", /AGENT_CORE_NODE_MAJOR must be >= 20/],
    ] as const) {
      await assert.rejects(
        execFileAsync("bash", [path.join("scripts", "bootstrap-macos.sh")], {
          env: {
            PATH: `${pathBin}:/usr/bin:/bin`,
            HOME: home,
            AGENT_CORE_NODE_MAJOR: major,
          },
        }),
        (error: unknown) => {
          const failed = error as { code?: number; stderr?: string };
          assert.equal(failed.code, 1);
          assert.match(failed.stderr ?? "", expected);
          return true;
        },
      );
    }
  } finally {
    await fs.rm(tempDir, { recursive: true, force: true });
  }
});

test("macOS bootstrap rejects unsafe Node distribution overrides", async () => {
  const tempDir = await makeTempDir("agent-core-bootstrap-node-dist-");
  const pathBin = path.join(tempDir, "path-bin");
  const home = path.join(tempDir, "home");

  try {
    await fs.mkdir(pathBin, { recursive: true });
    await fs.writeFile(
      path.join(pathBin, "uname"),
      "#!/usr/bin/env bash\nif [ \"${1:-}\" = \"-m\" ]; then printf 'arm64\\n'; else printf 'Darwin\\n'; fi\n",
      { mode: 0o755 },
    );
    await fs.writeFile(
      path.join(pathBin, "node"),
      "#!/usr/bin/env bash\nif [ \"${1:-}\" = \"-p\" ]; then printf '18\\n'; else exit 2; fi\n",
      { mode: 0o755 },
    );

    for (const [baseUrl, expected] of [
      ["http://node.invalid/dist/latest-v22.x", /AGENT_CORE_NODE_DIST_BASE must be an https:\/\/ URL/],
      ["https://node.invalid/dist latest-v22.x", /without whitespace/],
      ["https://node.invalid/dist/latest-v22.x\nhttps://other.invalid", /without whitespace/],
    ] as const) {
      await assert.rejects(
        execFileAsync("bash", [path.join("scripts", "bootstrap-macos.sh")], {
          env: {
            PATH: `${pathBin}:/usr/bin:/bin`,
            HOME: home,
            AGENT_CORE_PACKAGE: "file:///tmp/agent-core-test.tgz",
            AGENT_CORE_NODE_DIST_BASE: baseUrl,
          },
        }),
        (error: unknown) => {
          const failed = error as { code?: number; stderr?: string };
          assert.equal(failed.code, 1);
          assert.match(failed.stderr ?? "", expected);
          return true;
        },
      );
    }
  } finally {
    await fs.rm(tempDir, { recursive: true, force: true });
  }
});

test("macOS bootstrap rejects unsafe package specs", async () => {
  const tempDir = await makeTempDir("agent-core-bootstrap-package-spec-");
  const pathBin = path.join(tempDir, "path-bin");
  const home = path.join(tempDir, "home");

  try {
    await fs.mkdir(pathBin, { recursive: true });
    await fs.writeFile(
      path.join(pathBin, "uname"),
      "#!/usr/bin/env bash\nif [ \"${1:-}\" = \"-m\" ]; then printf 'arm64\\n'; else printf 'Darwin\\n'; fi\n",
      { mode: 0o755 },
    );

    for (const [packageSpec, expected] of [
      ["agent-core beta", /AGENT_CORE_PACKAGE must be a non-empty npm package spec without whitespace/],
      [
        "git+https://github.com/example/agent-core.git\nnpm:other-package",
        /AGENT_CORE_PACKAGE must be a non-empty npm package spec without whitespace/,
      ],
      ["--ignore-scripts", /AGENT_CORE_PACKAGE must be a package spec, not an npm option/],
    ] as const) {
      await assert.rejects(
        execFileAsync("bash", [path.join("scripts", "bootstrap-macos.sh")], {
          env: {
            PATH: `${pathBin}:/usr/bin:/bin`,
            HOME: home,
            AGENT_CORE_PACKAGE: packageSpec,
          },
        }),
        (error: unknown) => {
          const failed = error as { code?: number; stderr?: string };
          assert.equal(failed.code, 1);
          assert.match(failed.stderr ?? "", expected);
          return true;
        },
      );
    }
  } finally {
    await fs.rm(tempDir, { recursive: true, force: true });
  }
});

test("macOS bootstrap validates npm script opt-in", async () => {
  const tempDir = await makeTempDir("agent-core-bootstrap-npm-scripts-");
  const pathBin = path.join(tempDir, "path-bin");
  const home = path.join(tempDir, "home");

  try {
    await fs.mkdir(pathBin, { recursive: true });
    await fs.writeFile(
      path.join(pathBin, "uname"),
      "#!/usr/bin/env bash\nif [ \"${1:-}\" = \"-m\" ]; then printf 'arm64\\n'; else printf 'Darwin\\n'; fi\n",
      { mode: 0o755 },
    );

    await assert.rejects(
      execFileAsync("bash", [path.join("scripts", "bootstrap-macos.sh")], {
        env: {
          PATH: `${pathBin}:/usr/bin:/bin`,
          HOME: home,
          AGENT_CORE_NPM_RUN_SCRIPTS: "yes",
        },
      }),
      (error: unknown) => {
        const failed = error as { code?: number; stderr?: string };
        assert.equal(failed.code, 1);
        assert.match(failed.stderr ?? "", /AGENT_CORE_NPM_RUN_SCRIPTS must be 0 or 1/);
        return true;
      },
    );
  } finally {
    await fs.rm(tempDir, { recursive: true, force: true });
  }
});

test("macOS bootstrap validates bootstrap CLI options before setup", async () => {
  const script = path.join("scripts", "bootstrap-macos.sh");

  const help = await execFileAsync("bash", [script, "--help"]);
  assert.match(help.stdout, /--package <spec>/);
  assert.equal(help.stderr, "");

  for (const [args, expected] of [
    [["--package"], /--package requires a value/],
    [["--unknown"], /Unknown bootstrap option/],
    [["extra"], /Unexpected positional argument/],
  ] as const) {
    await assert.rejects(
      execFileAsync("bash", [script, ...args]),
      (error: unknown) => {
        const failed = error as { code?: number; stderr?: string };
        assert.equal(failed.code, 1);
        assert.match(failed.stderr ?? "", expected);
        return true;
      },
    );
  }
});

test("macOS bootstrap uses the scoped default package source", async () => {
  const tempDir = await makeTempDir("agent-core-bootstrap-default-package-");
  const pathBin = path.join(tempDir, "path-bin");
  const home = path.join(tempDir, "home");
  const npmArgs = path.join(tempDir, "npm-args");

  try {
    await fs.mkdir(pathBin, { recursive: true });
    await fs.writeFile(
      path.join(pathBin, "uname"),
      "#!/usr/bin/env bash\nif [ \"${1:-}\" = \"-m\" ]; then printf 'arm64\\n'; else printf 'Darwin\\n'; fi\n",
      { mode: 0o755 },
    );
    await fs.writeFile(
      path.join(pathBin, "node"),
      "#!/usr/bin/env bash\nif [ \"${1:-}\" = \"-p\" ]; then printf '20\\n'; else exit 2; fi\n",
      { mode: 0o755 },
    );
    await fs.writeFile(
      path.join(pathBin, "npm"),
      [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        `printf '%s\\n' "$*" > ${shellQuote(npmArgs)}`,
        "exit 0",
        "",
      ].join("\n"),
      { mode: 0o755 },
    );

    await assert.rejects(
      execFileAsync("bash", [path.join("scripts", "bootstrap-macos.sh")], {
        env: {
          PATH: `${pathBin}:/usr/bin:/bin`,
          HOME: home,
        },
      }),
      (error: unknown) => {
        const failed = error as { code?: number; stderr?: string };
        assert.equal(failed.code, 1);
        assert.match(failed.stderr ?? "", /expected agent binary was not found/);
        assert.doesNotMatch(failed.stderr ?? "", /default npm package name/);
        return true;
      },
    );
    const args = await fs.readFile(npmArgs, "utf8");
    assert.match(args, /install -g --prefix /);
    assert.match(args, /--ignore-scripts/);
    assert.match(args, /@puffo-ai\/agent-core/);
  } finally {
    await fs.rm(tempDir, { recursive: true, force: true });
  }
});

test("macOS bootstrap rejects unsafe install prefixes", async () => {
  const tempDir = await makeTempDir("agent-core-bootstrap-prefix-");
  const pathBin = path.join(tempDir, "path-bin");
  const home = path.join(tempDir, "home");

  try {
    await fs.mkdir(pathBin, { recursive: true });
    await fs.writeFile(
      path.join(pathBin, "uname"),
      "#!/usr/bin/env bash\nif [ \"${1:-}\" = \"-m\" ]; then printf 'arm64\\n'; else printf 'Darwin\\n'; fi\n",
      { mode: 0o755 },
    );
    await fs.writeFile(
      path.join(pathBin, "node"),
      "#!/usr/bin/env bash\nif [ \"${1:-}\" = \"-p\" ]; then printf '20\\n'; else exit 2; fi\n",
      { mode: 0o755 },
    );
    await fs.writeFile(path.join(pathBin, "npm"), "#!/usr/bin/env bash\nexit 0\n", { mode: 0o755 });

    await assert.rejects(
      execFileAsync("bash", [path.join("scripts", "bootstrap-macos.sh")], {
        env: {
          PATH: `${pathBin}:/usr/bin:/bin`,
          HOME: home,
          AGENT_CORE_INSTALL_PREFIX: "/",
        },
      }),
      (error: unknown) => {
        const failed = error as { code?: number; stderr?: string };
        assert.equal(failed.code, 1);
        assert.match(failed.stderr ?? "", /Refusing unsafe agent-core install prefix/);
        return true;
      },
    );
  } finally {
    await fs.rm(tempDir, { recursive: true, force: true });
  }
});

async function assertGitIgnored(relativePath: string): Promise<void> {
  const result = await execFileAsync("git", ["check-ignore", "-v", relativePath]);
  assert.match(result.stdout.trim(), new RegExp(`${escapeRegExp(relativePath)}$`));
}

async function assertGitNotIgnored(relativePath: string): Promise<void> {
  await assert.rejects(
    execFileAsync("git", ["check-ignore", "-v", relativePath]),
    (error: unknown) => {
      const failed = error as { code?: number; stdout?: string; stderr?: string };
      assert.equal(failed.code, 1);
      assert.equal(failed.stdout ?? "", "");
      return true;
    },
  );
}

async function assertRootGitIgnored(relativePath: string): Promise<void> {
  const result = await execFileAsync("git", ["check-ignore", "--no-index", "-v", relativePath], {
    cwd: path.resolve(".."),
  });
  assert.match(result.stdout.trim(), new RegExp(`${escapeRegExp(relativePath)}$`));
}

async function assertRootGitNotIgnored(relativePath: string): Promise<void> {
  await assert.rejects(
    execFileAsync("git", ["check-ignore", "--no-index", "-v", relativePath], {
      cwd: path.resolve(".."),
    }),
    (error: unknown) => {
      const failed = error as { code?: number; stdout?: string; stderr?: string };
      assert.equal(failed.code, 1);
      assert.equal(failed.stdout ?? "", "");
      return true;
    },
  );
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

interface NpmCommand {
  command: string;
  args: string[];
}

async function resolveNpmCommand(): Promise<NpmCommand> {
  const npmCli = process.env.npm_execpath;
  if (npmCli) return { command: process.execPath, args: [npmCli] };

  const result = await execFileAsync("which", ["npm"]);
  const command = result.stdout.trim().split(/\r?\n/)[0];
  if (!command) throw new Error("npm executable is required to run npm lifecycle tests");
  return { command, args: [] };
}

async function writeFakeNpm(directory: string, marker: string): Promise<void> {
  const script = path.join(directory, "npm");
  await fs.writeFile(
    script,
    [
      "#!/usr/bin/env bash",
      "set -euo pipefail",
      "if [ \"${1:-}\" = \"run\" ] && [ \"${2:-}\" = \"check:package\" ]; then",
      `  printf 'ran\\n' > ${shellQuote(marker)}`,
      "  printf 'fake check:package ok\\n'",
      "  exit 0",
      "fi",
      "printf 'unexpected fake npm args: %s\\n' \"$*\" >&2",
      "exit 77",
      "",
    ].join("\n"),
    { mode: 0o755 },
  );
}

async function fileExists(filePath: string): Promise<boolean> {
  try {
    await fs.access(filePath);
    return true;
  } catch {
    return false;
  }
}

function shellQuote(value: string): string {
  return `'${value.replace(/'/g, "'\\''")}'`;
}

test("macOS bootstrap rejects symlinked install prefixes", async () => {
  const tempDir = await makeTempDir("agent-core-bootstrap-symlink-prefix-");
  const pathBin = path.join(tempDir, "path-bin");
  const home = path.join(tempDir, "home");
  const targetPrefix = path.join(tempDir, "target-prefix");
  const installPrefix = path.join(tempDir, "linked-prefix");

  try {
    await fs.mkdir(pathBin, { recursive: true });
    await fs.mkdir(targetPrefix, { recursive: true });
    await fs.symlink(targetPrefix, installPrefix);
    await fs.writeFile(
      path.join(pathBin, "uname"),
      "#!/usr/bin/env bash\nif [ \"${1:-}\" = \"-m\" ]; then printf 'arm64\\n'; else printf 'Darwin\\n'; fi\n",
      { mode: 0o755 },
    );
    await fs.writeFile(
      path.join(pathBin, "node"),
      "#!/usr/bin/env bash\nif [ \"${1:-}\" = \"-p\" ]; then printf '20\\n'; else exit 2; fi\n",
      { mode: 0o755 },
    );
    await fs.writeFile(path.join(pathBin, "npm"), "#!/usr/bin/env bash\nexit 0\n", { mode: 0o755 });

    await assert.rejects(
      execFileAsync("bash", [path.join("scripts", "bootstrap-macos.sh")], {
        env: {
          PATH: `${pathBin}:/usr/bin:/bin`,
          HOME: home,
          AGENT_CORE_INSTALL_PREFIX: installPrefix,
        },
      }),
      (error: unknown) => {
        const failed = error as { code?: number; stderr?: string };
        assert.equal(failed.code, 1);
        assert.match(failed.stderr ?? "", /Refusing symlinked agent-core install prefix/);
        return true;
      },
    );
  } finally {
    await fs.rm(tempDir, { recursive: true, force: true });
  }
});

test("macOS bootstrap rejects unsafe Node install directories", async () => {
  const tempDir = await makeTempDir("agent-core-bootstrap-node-dir-");
  const pathBin = path.join(tempDir, "path-bin");
  const home = path.join(tempDir, "home");

  try {
    await fs.mkdir(pathBin, { recursive: true });
    await fs.writeFile(
      path.join(pathBin, "uname"),
      "#!/usr/bin/env bash\nif [ \"${1:-}\" = \"-m\" ]; then printf 'arm64\\n'; else printf 'Darwin\\n'; fi\n",
      { mode: 0o755 },
    );
    await fs.writeFile(
      path.join(pathBin, "node"),
      "#!/usr/bin/env bash\nif [ \"${1:-}\" = \"-p\" ]; then printf '18\\n'; else exit 2; fi\n",
      { mode: 0o755 },
    );

    await assert.rejects(
      execFileAsync("bash", [path.join("scripts", "bootstrap-macos.sh")], {
        env: {
          PATH: `${pathBin}:/usr/bin:/bin`,
          HOME: home,
          AGENT_CORE_PACKAGE: "file:///tmp/agent-core-test.tgz",
          AGENT_CORE_NODE_DIR: "/",
        },
      }),
      (error: unknown) => {
        const failed = error as { code?: number; stderr?: string };
        assert.equal(failed.code, 1);
        assert.match(failed.stderr ?? "", /Refusing unsafe Node.js install directory/);
        return true;
      },
    );
  } finally {
    await fs.rm(tempDir, { recursive: true, force: true });
  }
});

test("macOS bootstrap rejects symlinked Node install directories", async () => {
  const tempDir = await makeTempDir("agent-core-bootstrap-symlink-node-dir-");
  const pathBin = path.join(tempDir, "path-bin");
  const home = path.join(tempDir, "home");
  const targetNodeDir = path.join(tempDir, "target-node");
  const nodeDir = path.join(tempDir, "linked-node");

  try {
    await fs.mkdir(pathBin, { recursive: true });
    await fs.mkdir(targetNodeDir, { recursive: true });
    await fs.symlink(targetNodeDir, nodeDir);
    await fs.writeFile(
      path.join(pathBin, "uname"),
      "#!/usr/bin/env bash\nif [ \"${1:-}\" = \"-m\" ]; then printf 'arm64\\n'; else printf 'Darwin\\n'; fi\n",
      { mode: 0o755 },
    );
    await fs.writeFile(
      path.join(pathBin, "node"),
      "#!/usr/bin/env bash\nif [ \"${1:-}\" = \"-p\" ]; then printf '18\\n'; else exit 2; fi\n",
      { mode: 0o755 },
    );

    await assert.rejects(
      execFileAsync("bash", [path.join("scripts", "bootstrap-macos.sh")], {
        env: {
          PATH: `${pathBin}:/usr/bin:/bin`,
          HOME: home,
          AGENT_CORE_PACKAGE: "file:///tmp/agent-core-test.tgz",
          AGENT_CORE_NODE_DIR: nodeDir,
        },
      }),
      (error: unknown) => {
        const failed = error as { code?: number; stderr?: string };
        assert.equal(failed.code, 1);
        assert.match(failed.stderr ?? "", /Refusing symlinked Node.js install directory/);
        return true;
      },
    );
  } finally {
    await fs.rm(tempDir, { recursive: true, force: true });
  }
});

test("macOS bootstrap rejects symlinked temporary download directories", async () => {
  const tempDir = await makeTempDir("agent-core-bootstrap-symlink-temp-");
  const pathBin = path.join(tempDir, "path-bin");
  const home = path.join(tempDir, "home");
  const agentHome = path.join(home, ".agent-core");
  const targetTemp = path.join(tempDir, "target-temp");
  const tempRoot = path.join(agentHome, "tmp");

  try {
    await fs.mkdir(pathBin, { recursive: true });
    await fs.mkdir(agentHome, { recursive: true });
    await fs.mkdir(targetTemp, { recursive: true });
    await fs.symlink(targetTemp, tempRoot);
    await fs.writeFile(
      path.join(pathBin, "uname"),
      "#!/usr/bin/env bash\nif [ \"${1:-}\" = \"-m\" ]; then printf 'arm64\\n'; else printf 'Darwin\\n'; fi\n",
      { mode: 0o755 },
    );
    await fs.writeFile(
      path.join(pathBin, "node"),
      "#!/usr/bin/env bash\nif [ \"${1:-}\" = \"-p\" ]; then printf '18\\n'; else exit 2; fi\n",
      { mode: 0o755 },
    );

    await assert.rejects(
      execFileAsync("bash", [path.join("scripts", "bootstrap-macos.sh")], {
        env: {
          PATH: `${pathBin}:/usr/bin:/bin`,
          HOME: home,
          AGENT_CORE_PACKAGE: "file:///tmp/agent-core-test.tgz",
        },
      }),
      (error: unknown) => {
        const failed = error as { code?: number; stderr?: string };
        assert.equal(failed.code, 1);
        assert.match(failed.stderr ?? "", /Refusing symlinked temporary download directory/);
        return true;
      },
    );
  } finally {
    await fs.rm(tempDir, { recursive: true, force: true });
  }
});

test("macOS bootstrap starts agent from its user-local install prefix", async () => {
  const tempDir = await makeTempDir("agent-core-bootstrap-");
  const pathBin = path.join(tempDir, "path-bin");
  const home = path.join(tempDir, "home");
  const installPrefix = path.join(home, ".agent-core", "npm");

  try {
    await fs.mkdir(pathBin, { recursive: true });
    await fs.mkdir(path.join(installPrefix, "bin"), { recursive: true });

    await fs.writeFile(
      path.join(pathBin, "uname"),
      "#!/usr/bin/env bash\nif [ \"${1:-}\" = \"-m\" ]; then printf 'arm64\\n'; else printf 'Darwin\\n'; fi\n",
      { mode: 0o755 },
    );
    await fs.writeFile(
      path.join(pathBin, "node"),
      "#!/usr/bin/env bash\nif [ \"${1:-}\" = \"-p\" ]; then printf '20\\n'; else exit 2; fi\n",
      { mode: 0o755 },
    );
    await fs.writeFile(
      path.join(pathBin, "npm"),
      [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "if [ \"${1:-}\" = \"install\" ]; then",
        "  shift",
        "  prefix=''",
        "  package=''",
        "  saw_ignore=0",
        "  while [ \"$#\" -gt 0 ]; do",
        "    case \"$1\" in",
        "      -g) shift ;;",
        "      --prefix) prefix=\"${2:-}\"; shift 2 ;;",
        "      --ignore-scripts) saw_ignore=1; shift ;;",
        "      *) package=\"$1\"; shift ;;",
        "    esac",
        "  done",
        "  if [ \"$saw_ignore\" != '1' ]; then printf 'npm install missing --ignore-scripts\\n' >&2; exit 8; fi",
        "  printf 'fake npm installed %s into %s\\n' \"$package\" \"$prefix\"",
        "  printf 'fake npm umask %s\\n' \"$(umask)\"",
        "  exit 0",
        "fi",
        "if [ \"${1:-}\" = \"prefix\" ] && [ \"${2:-}\" = \"-g\" ]; then",
        "  printf '%s\\n' '/unused/system/prefix'",
        "  exit 0",
        "fi",
        "printf 'unexpected npm args: %s\\n' \"$*\" >&2",
        "exit 2",
        "",
      ].join("\n"),
      { mode: 0o755 },
    );
    await fs.writeFile(
      path.join(installPrefix, "bin", "agent"),
      "#!/usr/bin/env bash\nprintf 'agent %s\\n' \"$*\"\n",
      { mode: 0o755 },
    );

    const result = await execFileAsync("bash", [path.join("scripts", "bootstrap-macos.sh")], {
      env: {
        PATH: `${pathBin}:/usr/bin:/bin`,
        HOME: home,
        AGENT_CORE_PACKAGE: "file:///tmp/agent-core-test.tgz",
      },
    });

    assert.ok(result.stdout.includes(`Installing file:///tmp/agent-core-test.tgz into ${installPrefix}`));
    assert.ok(result.stdout.includes(`fake npm installed file:///tmp/agent-core-test.tgz into ${installPrefix}`));
    assert.match(result.stdout, /^fake npm umask 0077$/m);
    assert.match(result.stdout, /Starting local agent daemon/);
    assert.match(result.stdout, /^agent start$/m);
    assert.equal(result.stderr, "");
  } finally {
    await fs.rm(tempDir, { recursive: true, force: true });
  }
});

test("macOS bootstrap can opt into npm scripts for trusted source package overrides", async () => {
  const tempDir = await makeTempDir("agent-core-bootstrap-run-scripts-");
  const pathBin = path.join(tempDir, "path-bin");
  const home = path.join(tempDir, "home");
  const installPrefix = path.join(home, ".agent-core", "npm");

  try {
    await fs.mkdir(pathBin, { recursive: true });

    await fs.writeFile(
      path.join(pathBin, "uname"),
      "#!/usr/bin/env bash\nif [ \"${1:-}\" = \"-m\" ]; then printf 'arm64\\n'; else printf 'Darwin\\n'; fi\n",
      { mode: 0o755 },
    );
    await fs.writeFile(
      path.join(pathBin, "node"),
      "#!/usr/bin/env bash\nif [ \"${1:-}\" = \"-p\" ]; then printf '20\\n'; else exit 2; fi\n",
      { mode: 0o755 },
    );
    await fs.writeFile(
      path.join(pathBin, "npm"),
      [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "if [ \"${1:-}\" = \"install\" ]; then",
        "  shift",
        "  prefix=''",
        "  package=''",
        "  saw_ignore=0",
        "  while [ \"$#\" -gt 0 ]; do",
        "    case \"$1\" in",
        "      -g) shift ;;",
        "      --prefix) prefix=\"${2:-}\"; shift 2 ;;",
        "      --ignore-scripts) saw_ignore=1; shift ;;",
        "      *) package=\"$1\"; shift ;;",
        "    esac",
        "  done",
        "  if [ \"$saw_ignore\" = '1' ]; then printf 'npm install unexpectedly disabled scripts\\n' >&2; exit 8; fi",
        "  mkdir -p \"$prefix/bin\"",
        "  cat > \"$prefix/bin/agent\" <<'AGENT'",
        "#!/usr/bin/env bash",
        "printf 'agent %s\\n' \"$*\"",
        "AGENT",
        "  chmod +x \"$prefix/bin/agent\"",
        "  printf 'fake npm ran scripts for %s into %s\\n' \"$package\" \"$prefix\"",
        "  exit 0",
        "fi",
        "printf 'unexpected npm args: %s\\n' \"$*\" >&2",
        "exit 2",
        "",
      ].join("\n"),
      { mode: 0o755 },
    );

    const result = await execFileAsync("bash", [
      path.join("scripts", "bootstrap-macos.sh"),
      "--package",
      "git+https://github.com/example/agent-core.git#subdirectory=agent-core",
      "--run-scripts",
    ], {
      env: {
        PATH: `${pathBin}:/usr/bin:/bin`,
        HOME: home,
      },
    });

    assert.ok(
      result.stdout.includes(
        `fake npm ran scripts for git+https://github.com/example/agent-core.git#subdirectory=agent-core into ${installPrefix}`,
      ),
    );
    assert.match(result.stdout, /^agent start$/m);
    assert.equal(result.stderr, "");
  } finally {
    await fs.rm(tempDir, { recursive: true, force: true });
  }
});

test("macOS bootstrap refuses to start an agent binary from PATH", async () => {
  const tempDir = await makeTempDir("agent-core-bootstrap-path-agent-");
  const pathBin = path.join(tempDir, "path-bin");
  const home = path.join(tempDir, "home");
  const installPrefix = path.join(home, ".agent-core", "npm");

  try {
    await fs.mkdir(pathBin, { recursive: true });

    await fs.writeFile(
      path.join(pathBin, "uname"),
      "#!/usr/bin/env bash\nif [ \"${1:-}\" = \"-m\" ]; then printf 'arm64\\n'; else printf 'Darwin\\n'; fi\n",
      { mode: 0o755 },
    );
    await fs.writeFile(
      path.join(pathBin, "node"),
      "#!/usr/bin/env bash\nif [ \"${1:-}\" = \"-p\" ]; then printf '20\\n'; else exit 2; fi\n",
      { mode: 0o755 },
    );
    await fs.writeFile(
      path.join(pathBin, "npm"),
      [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "if [ \"${1:-}\" = \"install\" ]; then",
        "  shift",
        "  prefix=''",
        "  saw_ignore=0",
        "  while [ \"$#\" -gt 0 ]; do",
        "    case \"$1\" in",
        "      -g) shift ;;",
        "      --prefix) prefix=\"${2:-}\"; shift 2 ;;",
        "      --ignore-scripts) saw_ignore=1; shift ;;",
        "      *) shift ;;",
        "    esac",
        "  done",
        "  if [ \"$saw_ignore\" != '1' ]; then printf 'npm install missing --ignore-scripts\\n' >&2; exit 8; fi",
        "  printf 'fake npm installed without agent into %s\\n' \"$prefix\"",
        "  exit 0",
        "fi",
        "printf 'unexpected npm args: %s\\n' \"$*\" >&2",
        "exit 2",
        "",
      ].join("\n"),
      { mode: 0o755 },
    );
    await fs.writeFile(
      path.join(pathBin, "agent"),
      "#!/usr/bin/env bash\nprintf 'wrong path agent %s\\n' \"$*\"\n",
      { mode: 0o755 },
    );

    await assert.rejects(
      execFileAsync("bash", [path.join("scripts", "bootstrap-macos.sh")], {
        env: {
          PATH: `${pathBin}:/usr/bin:/bin`,
          HOME: home,
          AGENT_CORE_PACKAGE: "file:///tmp/agent-core-test.tgz",
        },
      }),
      (error: unknown) => {
        const failed = error as { code?: number; stdout?: string; stderr?: string };
        assert.equal(failed.code, 1);
        assert.match(failed.stdout ?? "", /fake npm installed without agent/);
        assert.doesNotMatch(failed.stdout ?? "", /wrong path agent/);
        assert.match(failed.stderr ?? "", /will not start a different agent binary from PATH/);
        assert.match(failed.stderr ?? "", new RegExp(`${escapeRegExp(installPrefix)}/bin/agent`));
        return true;
      },
    );
  } finally {
    await fs.rm(tempDir, { recursive: true, force: true });
  }
});

test("macOS bootstrap removes temporary Node downloads on failure", async () => {
  const tempDir = await makeTempDir("agent-core-bootstrap-node-cleanup-");
  const pathBin = path.join(tempDir, "path-bin");
  const home = path.join(tempDir, "home");
  const tempRoot = path.join(home, ".agent-core", "tmp");

  try {
    await fs.mkdir(pathBin, { recursive: true });

    await fs.writeFile(
      path.join(pathBin, "uname"),
      "#!/usr/bin/env bash\nif [ \"${1:-}\" = \"-m\" ]; then printf 'arm64\\n'; else printf 'Darwin\\n'; fi\n",
      { mode: 0o755 },
    );
    await fs.writeFile(
      path.join(pathBin, "node"),
      "#!/usr/bin/env bash\nif [ \"${1:-}\" = \"-p\" ]; then printf '18\\n'; else exit 2; fi\n",
      { mode: 0o755 },
    );
    await fs.writeFile(
      path.join(pathBin, "curl"),
      [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "if [[ \"$*\" == *SHASUMS256.txt* ]]; then",
        "  printf '%s  %s\\n' 'fake-sha' 'node-v22.99.0-darwin-arm64.tar.gz'",
        "  exit 0",
        "fi",
        "printf 'archive download failed\\n' >&2",
        "exit 7",
        "",
      ].join("\n"),
      { mode: 0o755 },
    );
    await fs.writeFile(
      path.join(pathBin, "shasum"),
      "#!/usr/bin/env bash\nprintf '%s  %s\\n' 'fake-sha' \"${3:-}\"\n",
      { mode: 0o755 },
    );

    await assert.rejects(
      execFileAsync("bash", [path.join("scripts", "bootstrap-macos.sh")], {
        env: {
          PATH: `${pathBin}:/usr/bin:/bin`,
          HOME: home,
          AGENT_CORE_PACKAGE: "file:///tmp/agent-core-test.tgz",
          AGENT_CORE_NODE_DIST_BASE: "https://node.invalid/dist/latest-v22.x",
        },
      }),
      (error: unknown) => {
        const failed = error as { code?: number; stderr?: string };
        assert.equal(failed.code, 7);
        assert.match(failed.stderr ?? "", /archive download failed/);
        return true;
      },
    );

    const entries = await fs.readdir(tempRoot).catch(() => []);
    assert.deepEqual(entries.filter((entry) => entry.startsWith("node.")), []);
  } finally {
    await fs.rm(tempDir, { recursive: true, force: true });
  }
});

test("macOS bootstrap installs user-local Node when Node is missing", async () => {
  const tempDir = await makeTempDir("agent-core-bootstrap-node-");
  const pathBin = path.join(tempDir, "path-bin");
  const home = path.join(tempDir, "home");
  const installPrefix = path.join(home, ".agent-core", "npm");
  const nodeDir = path.join(home, ".agent-core", "node");

  try {
    await fs.mkdir(pathBin, { recursive: true });

    await fs.writeFile(
      path.join(pathBin, "uname"),
      "#!/usr/bin/env bash\nif [ \"${1:-}\" = \"-m\" ]; then printf 'arm64\\n'; else printf 'Darwin\\n'; fi\n",
      { mode: 0o755 },
    );
    await fs.writeFile(
      path.join(pathBin, "curl"),
      [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "if [[ \"$*\" != *'--proto =https --proto-redir =https'* ]]; then",
        "  printf 'curl missing https protocol restrictions: %s\\n' \"$*\" >&2",
        "  exit 9",
        "fi",
        "if [[ \"$*\" == *SHASUMS256.txt* ]]; then",
        "  printf '%s  %s\\n' 'fake-sha' 'node-v22.99.0-darwin-arm64.tar.gz'",
        "  exit 0",
        "fi",
        "out=''",
        "while [ \"$#\" -gt 0 ]; do",
        "  case \"$1\" in",
        "    -o) out=\"$2\"; shift 2 ;;",
        "    -*) shift ;;",
        "    *) shift ;;",
        "  esac",
        "done",
        "if [ -n \"$out\" ]; then printf 'fake node archive' > \"$out\"; else printf 'fake node archive'; fi",
        "",
      ].join("\n"),
      { mode: 0o755 },
    );
    await fs.writeFile(
      path.join(pathBin, "shasum"),
      "#!/usr/bin/env bash\nprintf '%s  %s\\n' 'fake-sha' \"${3:-}\"\n",
      { mode: 0o755 },
    );
    await fs.writeFile(
      path.join(pathBin, "tar"),
      [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "dest=''",
        "while [ \"$#\" -gt 0 ]; do",
        "  if [ \"$1\" = '-C' ]; then dest=\"$2\"; shift 2; else shift; fi",
        "done",
        "mkdir -p \"$dest/bin\"",
        "cat > \"$dest/bin/node\" <<'NODE'",
        "#!/usr/bin/env bash",
        "if [ \"${1:-}\" = \"-p\" ]; then printf '22\\n'; else exit 2; fi",
        "NODE",
        "chmod +x \"$dest/bin/node\"",
        "cat > \"$dest/bin/npm\" <<'NPM'",
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "if [ \"${1:-}\" = \"install\" ]; then",
        "  shift",
        "  prefix=''",
        "  package=''",
        "  saw_ignore=0",
        "  while [ \"$#\" -gt 0 ]; do",
        "    case \"$1\" in",
        "      -g) shift ;;",
        "      --prefix) prefix=\"${2:-}\"; shift 2 ;;",
        "      --ignore-scripts) saw_ignore=1; shift ;;",
        "      *) package=\"$1\"; shift ;;",
        "    esac",
        "  done",
        "  if [ \"$saw_ignore\" != '1' ]; then printf 'npm install missing --ignore-scripts\\n' >&2; exit 8; fi",
        "  mkdir -p \"$prefix/bin\"",
        "  cat > \"$prefix/bin/agent\" <<'AGENT'",
        "#!/usr/bin/env bash",
        "printf 'agent %s\\n' \"$*\"",
        "AGENT",
        "  chmod +x \"$prefix/bin/agent\"",
        "  printf 'fake npm installed %s into %s\\n' \"$package\" \"$prefix\"",
        "  printf 'fake npm umask %s\\n' \"$(umask)\"",
        "  exit 0",
        "fi",
        "printf 'unexpected npm args: %s\\n' \"$*\" >&2",
        "exit 2",
        "NPM",
        "chmod +x \"$dest/bin/npm\"",
        "",
      ].join("\n"),
      { mode: 0o755 },
    );

    const result = await execFileAsync("bash", [path.join("scripts", "bootstrap-macos.sh")], {
      env: {
        PATH: `${pathBin}:/usr/bin:/bin`,
        HOME: home,
        AGENT_CORE_PACKAGE: "file:///tmp/agent-core-test.tgz",
        AGENT_CORE_NODE_DIST_BASE: "https://node.invalid/dist/latest-v22.x",
      },
    });

    assert.ok(result.stdout.includes(`Installing Node.js 22.x for darwin-arm64 into ${nodeDir}`));
    assert.ok(result.stdout.includes(`Installing file:///tmp/agent-core-test.tgz into ${installPrefix}`));
    assert.ok(result.stdout.includes(`fake npm installed file:///tmp/agent-core-test.tgz into ${installPrefix}`));
    assert.match(result.stdout, /^fake npm umask 0077$/m);
    assert.match(result.stdout, /^agent start$/m);
    assert.equal(result.stderr, "");
  } finally {
    await fs.rm(tempDir, { recursive: true, force: true });
  }
});
