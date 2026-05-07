const MIN_PAIRING_PUBLIC_NONCE_LENGTH = 32;
const MAX_PAIRING_PUBLIC_NONCE_LENGTH = 128;

export function isValidPairingPublicNonce(value: unknown): value is string {
  return (
    typeof value === "string" &&
    value.length >= MIN_PAIRING_PUBLIC_NONCE_LENGTH &&
    value.length <= MAX_PAIRING_PUBLIC_NONCE_LENGTH &&
    /^[A-Za-z0-9_-]+$/.test(value)
  );
}
