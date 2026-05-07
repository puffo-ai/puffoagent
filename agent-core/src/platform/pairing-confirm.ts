import { isValidPairingId } from "./pairing-id.js";
import { normalizePairingAuthToken } from "./pairing-token.js";

export interface NormalizedPairingConfirmInput {
  [key: string]: unknown;
  authToken: string;
  pairingId?: string;
}

export function normalizePairingConfirmInput(input: Record<string, unknown>): NormalizedPairingConfirmInput {
  if (!input || typeof input !== "object" || Array.isArray(input)) {
    throw new Error("pairing confirmation body must be an object");
  }
  const unsupported = Object.keys(input).find((key) => key !== "authToken" && key !== "pairingId");
  if (unsupported) throw new Error(`${unsupported} is not supported for pairing confirmation`);
  const authToken = normalizePairingAuthToken(input.authToken);
  if (!authToken) throw new Error("authToken must be a non-empty string up to 16384 characters");
  if ("pairingId" in input && typeof input.pairingId !== "string") {
    throw new Error("pairingId must be a string");
  }
  if (typeof input.pairingId === "string" && !isValidPairingId(input.pairingId)) {
    throw new Error("pairingId must be a safe identifier");
  }
  return {
    authToken,
    ...(typeof input.pairingId === "string" ? { pairingId: input.pairingId } : {}),
  };
}
