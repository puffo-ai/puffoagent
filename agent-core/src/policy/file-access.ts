export interface FileAccessPolicy {
  readablePaths: string[];
  writablePaths: string[];
}

export interface FileAccessInput {
  readablePaths?: string[];
  writablePaths?: string[];
}

export function emptyFileAccess(): FileAccessPolicy {
  return { readablePaths: [], writablePaths: [] };
}

export function normalizeFileAccess(value: unknown): FileAccessPolicy {
  if (!value || typeof value !== "object" || Array.isArray(value)) return emptyFileAccess();
  const input = value as Partial<FileAccessInput>;
  return {
    readablePaths: normalizePathList(input.readablePaths),
    writablePaths: normalizePathList(input.writablePaths),
  };
}

export function mergeFileAccess(
  current: FileAccessPolicy | undefined,
  update: FileAccessInput | null | undefined,
): FileAccessPolicy {
  if (update === null) return emptyFileAccess();
  if (update === undefined) return normalizeFileAccess(current);
  const base = normalizeFileAccess(current);
  return {
    readablePaths:
      update.readablePaths === undefined ? base.readablePaths : normalizePathList(update.readablePaths),
    writablePaths:
      update.writablePaths === undefined ? base.writablePaths : normalizePathList(update.writablePaths),
  };
}

export function fileAccessHasEntries(value: FileAccessPolicy | undefined): boolean {
  const access = normalizeFileAccess(value);
  return access.readablePaths.length > 0 || access.writablePaths.length > 0;
}

function normalizePathList(value: unknown): string[] {
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
