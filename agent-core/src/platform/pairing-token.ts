const MAX_PAIRING_AUTH_TOKEN_LENGTH = 16 * 1024;

export function normalizePairingAuthToken(value: unknown): string | undefined {
  if (typeof value !== "string") return undefined;
  const token = value.trim();
  if (!token || token.length > MAX_PAIRING_AUTH_TOKEN_LENGTH) return undefined;
  return token;
}
