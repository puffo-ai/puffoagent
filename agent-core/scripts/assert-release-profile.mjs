#!/usr/bin/env node

const profile = process.env.AGENT_CORE_NATIVE_BUILD_PROFILE;

if (profile === "prod") {
  process.exit(0);
}

console.error(
  [
    "Refusing to publish agent-core with the default dev native sidecar.",
    "Set AGENT_CORE_NATIVE_BUILD_PROFILE=prod before npm publish so prepack stages the production-profile sidecar.",
    "For local test tarballs, use npm pack or npm run smoke:package instead of npm publish.",
  ].join("\n"),
);
process.exit(1);
