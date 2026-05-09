export type NetworkAccess = "inherit" | "deny";

export function parseNetworkAccess(value: unknown, fallback = defaultNetworkAccess()): NetworkAccess {
  if (value === "inherit" || value === "deny") return value;
  return fallback;
}

export function defaultNetworkAccess(): NetworkAccess {
  return process.env.AGENT_CORE_NETWORK === "off" ? "deny" : "inherit";
}
