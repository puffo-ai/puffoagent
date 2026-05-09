export const MAX_DECLARED_OPERATOR_PUBLIC_KEY_LENGTH = 4096;

export function normalizeDeclaredOperatorPublicKey(value: unknown): string | undefined {
  if (typeof value !== "string") return undefined;
  const normalized = value.trim();
  if (!normalized || normalized.length > MAX_DECLARED_OPERATOR_PUBLIC_KEY_LENGTH) return undefined;
  return normalized;
}
