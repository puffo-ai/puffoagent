import crypto from "node:crypto";
import { DEFAULT_API_HOST, DEFAULT_API_PORT } from "../config/defaults.js";
import { ApiServer } from "../api/server.js";
import { ProviderDetector } from "../doctor/provider-detector.js";
import { LogStore } from "../logs/log-store.js";
import { CoreNative, UnavailableCoreNative } from "../native/core.js";
import { CliCoreNative } from "../native/cli-core.js";
import { SidecarCoreNative } from "../native/sidecar-core.js";
import { ServerPairingGateway } from "../pairing/server-pairing.js";
import { RuntimeManager } from "../runtime/runtime-manager.js";
import { StateStore } from "../state/store.js";
import {
  ensureStateDir,
  daemonIsRunning,
  removeDaemonPid,
  writeDaemonPid,
} from "./lockfile.js";
import { DaemonSupervisor } from "./supervisor.js";

export interface DaemonInstance {
  api: ApiServer;
  store: StateStore;
  runtime: RuntimeManager;
  supervisor: DaemonSupervisor;
  host: string;
  port: number;
  close(): Promise<void>;
}

export async function startDaemon(options: { host?: string; port?: number; stateHome?: string } = {}): Promise<DaemonInstance> {
  const host = options.host ?? DEFAULT_API_HOST;
  const port = options.port ?? DEFAULT_API_PORT;
  const store = new StateStore(options.stateHome);
  await ensureStateDir(store);
  await store.init();
  const existing = await store.readDaemon();
  const existingPid = existing?.pid;
  if (existingPid && (await daemonIsRunning(store))) {
    throw new Error(`agent daemon is already running with pid ${existingPid}`);
  }
  if (existingPid) await removeDaemonPid(store);

  const device = await store.ensureDeviceState();
  const instanceId = crypto.randomUUID();
  const logs = new LogStore(store.paths.root);
  const detector = new ProviderDetector();
  const core = createCoreNative({ stateHome: store.paths.root });
  const pairing = new ServerPairingGateway(core, store);
  const runtime = new RuntimeManager(store, logs, core);
  const api = new ApiServer({
    store,
    detector,
    runtime,
    logs,
    core,
    pairing,
    apiToken: device.apiToken,
    devRoutes: process.env.AGENT_CORE_DEV_ROUTES === "1",
    instanceId,
  });
  const supervisor = new DaemonSupervisor(runtime);

  const server = await api.listen(port, host);
  const actualPort = serverPort(server, port);
  await writeDaemonPid(store, host, actualPort, instanceId);
  await supervisor.startPersistedRunningAgents();

  return {
    api,
    store,
    runtime,
    supervisor,
    host,
    port: actualPort,
    async close() {
      await supervisor.stopAll();
      await api.close();
      await core.shutdown?.();
      await removeDaemonPid(store);
    },
  };
}

export function createCoreNative(options: { stateHome?: string } = {}): CoreNative {
  if (process.env.AGENT_CORE_NATIVE === "unavailable") {
    return new UnavailableCoreNative();
  }
  if (process.env.AGENT_CORE_NATIVE === "cli") {
    return new CliCoreNative();
  }
  return new SidecarCoreNative(options.stateHome === undefined ? {} : { stateHome: options.stateHome });
}

function serverPort(server: import("node:http").Server, fallback: number): number {
  const address = server.address();
  if (address && typeof address === "object") return address.port;
  return fallback;
}
