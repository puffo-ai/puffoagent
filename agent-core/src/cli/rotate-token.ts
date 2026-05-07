import { daemonIsRunning, removeDaemonPid } from "../daemon/lockfile.js";
import { StateStore } from "../state/store.js";

export async function runRotateToken(): Promise<void> {
  const store = new StateStore();
  await store.init();
  const daemon = await store.readDaemon();
  if (daemon?.pid && (await daemonIsRunning(store))) {
    const token = (await store.readDeviceState())?.apiToken;
    if (!token) {
      throw new Error(
        "agent daemon is running but the local control token is missing or corrupt. Run `agent stop` and then `agent start` to recover it.",
      );
    }
    const rotated = await rotateThroughDaemon(daemon.host, daemon.port, token);
    printRotatedToken(rotated.token);
    return;
  }

  if (daemon?.pid) await removeDaemonPid(store);
  const token = await store.rotateLocalControlToken();
  printRotatedToken(token);
}

async function rotateThroughDaemon(host: string, port: number, token: string): Promise<{ token: string }> {
  const response = await fetch(`http://${hostForUrl(host)}:${port}/local-control-token/rotate`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: "{}",
  });
  const body = (await response.json()) as { token?: unknown; error?: { message?: string } };
  if (!response.ok) {
    throw new Error(body.error?.message || `failed to rotate local control token: HTTP ${response.status}`);
  }
  if (typeof body.token !== "string" || !body.token) {
    throw new Error("local control token rotation response did not include a token");
  }
  return { token: body.token };
}

function printRotatedToken(token: string): void {
  console.log("local control token rotated");
  console.log(`local control token: ${token}`);
  console.log("Existing local grants were revoked.");
}

function hostForUrl(host: string): string {
  return host.includes(":") && !host.startsWith("[") ? `[${host}]` : host;
}
