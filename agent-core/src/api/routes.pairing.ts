export const PAIRING_START_ROUTE = "/pairing/start";
export const PAIRING_CONFIRM_ROUTE = "/pairing/confirm";
export function pairingPollRoute(pairingId: string): string {
  return `/pairing/${encodeURIComponent(pairingId)}`;
}
