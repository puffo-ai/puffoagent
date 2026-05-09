import path from "node:path";
import { ProviderKind } from "../types.js";

const PROVIDER_CONFIG_PATHS: Record<ProviderKind, string[]> = {
  claude: [
    ".claude/agents",
    ".claude/commands",
    ".claude/settings.json",
    ".claude/settings.local.json",
    ".claude/skills",
  ],
  codex: [
    ".codex/config.toml",
    ".codex/prompts",
    ".codex/skills",
  ],
};

export function normalizeProviderConfigPaths(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return Array.from(
    new Set(
      value
        .filter((entry): entry is string => typeof entry === "string")
        .map((entry) => normalizeProviderConfigPath(entry))
        .filter((entry): entry is string => Boolean(entry)),
    ),
  );
}

export function normalizeProviderConfigPathsForProvider(provider: ProviderKind, value: unknown): string[] {
  return normalizeProviderConfigPaths(value).filter((entry) => isProviderOwnedConfigPath(provider, entry));
}

export function mergeProviderConfigPaths(
  current: string[] | undefined,
  update: string[] | null | undefined,
): string[] {
  if (update === null) return [];
  if (update === undefined) return normalizeProviderConfigPaths(current);
  return normalizeProviderConfigPaths(update);
}

export function isValidProviderConfigPath(value: unknown): value is string {
  return Boolean(normalizeProviderConfigPath(value));
}

export function isValidProviderConfigPathForProvider(provider: ProviderKind, value: unknown): value is string {
  const normalized = normalizeProviderConfigPath(value);
  return Boolean(normalized && isProviderOwnedConfigPath(provider, normalized));
}

export function normalizeProviderConfigPath(value: unknown): string | undefined {
  if (typeof value !== "string") return undefined;
  const trimmed = value.trim();
  if (!trimmed || trimmed.length > 512) return undefined;
  if (path.isAbsolute(trimmed)) return undefined;
  const normalized = path.normalize(trimmed);
  if (!normalized || normalized === "." || normalized.startsWith("..") || path.isAbsolute(normalized)) {
    return undefined;
  }
  if (normalized.split(path.sep).some((segment) => segment === ".." || segment === "")) return undefined;
  return normalized;
}

function isProviderOwnedConfigPath(provider: ProviderKind, value: string): boolean {
  return PROVIDER_CONFIG_PATHS[provider].some((root) => value === root || value.startsWith(`${root}${path.sep}`));
}
