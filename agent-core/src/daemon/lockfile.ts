import { ensureDirectory } from "../platform/safe-directory.js";
import { StateStore } from "../state/store.js";
import { DaemonState } from "../state/store.js";

export async function readDaemonPid(store: StateStore): Promise<number | undefined> {
  const state = await store.readDaemon();
  return state?.pid;
}

export async function daemonIsRunning(store: StateStore, timeoutMs = 500): Promise<boolean> {
  const state = await store.readDaemon();
  if (!state?.pid || !(await processIsRunning(state.pid))) return false;
  return daemonHealthMatches(state, store, timeoutMs);
}

export async function writeDaemonPid(store: StateStore, host: string, port: number, instanceId?: string): Promise<void> {
  await store.saveDaemon({
    pid: process.pid,
    host,
    port,
    startedAt: new Date().toISOString(),
    ...(instanceId ? { instanceId } : {}),
  });
}

export async function removeDaemonPid(store: StateStore): Promise<void> {
  await store.clearDaemon();
}

export async function processIsRunning(pid: number): Promise<boolean> {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

export async function ensureStateDir(store: StateStore): Promise<void> {
  await ensureDirectory(store.paths.root);
}

async function daemonHealthMatches(state: DaemonState, store: StateStore, timeoutMs: number): Promise<boolean> {
  if (!state.host || !state.port) return false;
  const device = await store.readDeviceState();
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(`http://${hostForUrl(state.host)}:${state.port}/health`, {
      signal: controller.signal,
      ...(device?.apiToken ? { headers: { "X-Agent-Core-Token": device.apiToken } } : {}),
    });
    if (!response.ok) return false;
    const body = (await response.json()) as { ok?: unknown; stateHome?: unknown; instanceId?: unknown };
    if (body.ok !== true) return false;
    if (state.instanceId && body.instanceId === state.instanceId) return true;
    return body.stateHome === store.paths.root;
  } catch {
    return false;
  } finally {
    clearTimeout(timeout);
  }
}

function hostForUrl(host: string): string {
  return host.includes(":") && !host.startsWith("[") ? `[${host}]` : host;
}
