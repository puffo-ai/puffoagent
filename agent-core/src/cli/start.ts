import { DAEMON_VERSION, DEFAULT_API_HOST, DEFAULT_API_PORT } from "../config/defaults.js";
import { startDaemon } from "../daemon/daemon.js";
import { installShutdownHandlers } from "../daemon/lifecycle.js";
import { daemonIsRunning } from "../daemon/lockfile.js";
import { StateStore } from "../state/store.js";

type StartStatus = "listening" | "already_listening";

export async function runStart(argv: string[]): Promise<void> {
  const port = readPort(argv) ?? readEnvPort() ?? DEFAULT_API_PORT;
  const json = argv.includes("--json");
  const store = new StateStore();
  await store.init();
  const existing = await store.readDaemon();
  if (existing?.pid && (await daemonIsRunning(store))) {
    const token = (await store.readDeviceState())?.apiToken;
    if (!token) {
      throw new Error(
        "agent daemon is already running but the local control token is missing or corrupt. Run `agent stop` and then `agent start` to regenerate it.",
      );
    }
    printConnection("already_listening", existing.host || DEFAULT_API_HOST, existing.port || port, token, json);
    return;
  }
  const daemon = await startDaemon({ host: DEFAULT_API_HOST, port });
  const token = (await daemon.store.ensureDeviceState()).apiToken;
  printConnection("listening", daemon.host, daemon.port, token, json);
  installShutdownHandlers(() => daemon.close());
}

function printConnection(status: StartStatus, host: string, port: number, token: string, json: boolean): void {
  const url = `http://${host}:${port}`;
  if (json) {
    console.log(
      JSON.stringify({
        status,
        ok: true,
        version: DAEMON_VERSION,
        host,
        port,
        url,
        token,
        authRequired: true,
        message: "Return to Web and click Re-check.",
      }),
    );
    return;
  }
  const label = status === "already_listening" ? "agent daemon already listening" : "agent daemon listening";
  console.log(`${label} on ${url}`);
  console.log(`local control token: ${token}`);
  console.log("Return to Web and click Re-check.");
}

function readPort(argv: string[]): number | undefined {
  const index = argv.indexOf("--port");
  if (index === -1) return undefined;
  const raw = argv[index + 1];
  if (!raw) throw new Error("--port requires a value");
  return parsePort(raw, "--port");
}

function readEnvPort(): number | undefined {
  const raw = process.env.AGENT_CORE_PORT;
  if (raw === undefined || raw === "") return undefined;
  return parsePort(raw, "AGENT_CORE_PORT");
}

function parsePort(raw: string, label: string): number {
  if (!/^\d+$/.test(raw)) throw new Error(`${label} must be an integer port from 1 to 65535`);
  const parsed = Number(raw);
  if (!Number.isSafeInteger(parsed) || parsed < 1 || parsed > 65_535) {
    throw new Error(`${label} must be an integer port from 1 to 65535`);
  }
  return parsed;
}
