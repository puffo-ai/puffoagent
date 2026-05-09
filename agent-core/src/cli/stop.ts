import { StateStore } from "../state/store.js";
import { daemonIsRunning, processIsRunning, removeDaemonPid } from "../daemon/lockfile.js";

export async function runStop(): Promise<void> {
  const store = new StateStore();
  const state = await store.readDaemon();
  if (!state?.pid) {
    await removeDaemonPid(store);
    console.log("agent daemon is not running");
    return;
  }
  const pid = state.pid;
  if (!(await daemonIsRunning(store))) {
    await removeDaemonPid(store);
    console.log("removed stale daemon pid file");
    return;
  }
  if (await processIsRunning(pid)) {
    process.kill(pid, "SIGTERM");
    console.log(`sent SIGTERM to agent daemon pid ${pid}`);
  }
}
