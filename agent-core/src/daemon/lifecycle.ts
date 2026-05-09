export function installShutdownHandlers(onShutdown: () => Promise<void>): void {
  let shuttingDown = false;
  const run = (): void => {
    if (shuttingDown) return;
    shuttingDown = true;
    void onShutdown().finally(() => process.exit(0));
  };
  process.once("SIGINT", run);
  process.once("SIGTERM", run);
}
