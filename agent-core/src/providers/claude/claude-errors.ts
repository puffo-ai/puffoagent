export function classifyClaudeError(stderr: string): string {
  if (/sandbox-exec|sandbox/i.test(stderr) && /command not found|not recognized|enoent|no such file/i.test(stderr)) {
    return "sandbox_unavailable";
  }
  if (/login|auth|credential|api key/i.test(stderr)) return "claude_not_logged_in";
  if (/command not found|not recognized|enoent|no such file/i.test(stderr)) return "claude_not_found";
  return "claude_failed";
}
