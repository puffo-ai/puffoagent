export function classifyCodexError(stderr: string): string {
  if (/sandbox-exec|sandbox/i.test(stderr) && /command not found|not recognized|enoent|no such file/i.test(stderr)) {
    return "sandbox_unavailable";
  }
  if (/login|auth|api key|credential/i.test(stderr)) return "codex_not_logged_in";
  if (/command not found|not recognized|enoent|no such file/i.test(stderr)) return "codex_not_found";
  return "codex_failed";
}
