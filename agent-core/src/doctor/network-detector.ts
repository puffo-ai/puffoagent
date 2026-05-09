export interface NetworkCheck {
  reachable: boolean;
  reason?: string;
}

export async function checkNetwork(url: string, timeoutMs = 3000): Promise<NetworkCheck> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, { method: "HEAD", signal: controller.signal });
    return { reachable: response.ok || response.status < 500 };
  } catch (error) {
    return { reachable: false, reason: error instanceof Error ? error.message : String(error) };
  } finally {
    clearTimeout(timeout);
  }
}
