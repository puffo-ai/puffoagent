export function normalizeDeniedTools(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return Array.from(
    new Set(
      value
        .filter((entry): entry is string => typeof entry === "string")
        .map((entry) => entry.trim())
        .filter(Boolean),
    ),
  );
}

export function isValidDeniedTool(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0 && value.length <= 512;
}
