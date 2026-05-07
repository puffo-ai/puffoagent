#!/usr/bin/env node
import { runDoctor } from "./doctor.js";
import { runRotateToken } from "./rotate-token.js";
import { runStart } from "./start.js";
import { runStop } from "./stop.js";
import { printVersion } from "./version.js";

const [, , command = "help", ...args] = process.argv;

try {
  if (command === "start") await runStart(args);
  else if (command === "doctor") await runDoctor();
  else if (command === "stop") await runStop();
  else if (command === "rotate-token") await runRotateToken();
  else if (command === "version" || command === "--version" || command === "-v") printVersion();
  else {
    console.log(`Usage:
  agent start [--port <port>] [--json]
  agent stop
  agent rotate-token
  agent doctor
  agent version`);
  }
} catch (error) {
  console.error(error instanceof Error ? error.message : String(error));
  process.exitCode = 1;
}
