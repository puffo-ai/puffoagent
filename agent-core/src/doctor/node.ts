export function nodeRuntime(): { version: string; major: number } {
  const major = Number.parseInt(process.version.replace(/^v/, "").split(".")[0] ?? "0", 10);
  return { version: process.version, major };
}
