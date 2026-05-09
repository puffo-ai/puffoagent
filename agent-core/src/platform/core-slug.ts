const CORE_SLUG = /^[a-z0-9][a-z0-9-]{0,62}$/;

export function isValidCoreSlug(value: unknown): value is string {
  return typeof value === "string" && CORE_SLUG.test(value);
}
