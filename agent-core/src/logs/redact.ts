const SECRET_PATTERNS: Array<[RegExp, string]> = [
  [/("[^"]*(?:api[_-]?key|token|secret|password)[^"]*"\s*:\s*")[^"]*(")/gi, "$1[redacted]$2"],
  [/(authorization:\s*bearer\s+)[^\s]+/gi, "$1[redacted]"],
  [/((?:api[_-]?key|token|secret|password)\s*[=:]\s*)[^\s,"']+/gi, "$1[redacted]"],
  [/((?:keychain|securityd)[^:\n]*(?:account|service|label)\s*[=:]\s*)[^\s,"']+/gi, "$1[redacted]"],
  [/(sk-[A-Za-z0-9_-]{12,})/g, "[redacted-api-key]"],
];

export function redact(input: string): string {
  return SECRET_PATTERNS.reduce(
    (value, [pattern, replacement]) => value.replace(pattern, replacement),
    input,
  );
}
