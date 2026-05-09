const PAIRING_ID = /^[A-Za-z0-9_-]{1,128}$/;

export function isValidPairingId(value: unknown): value is string {
  return typeof value === "string" && PAIRING_ID.test(value);
}
