"""The (runtime, provider, harness) validity matrix.

Every puffoagent agent picks three orthogonal knobs:

  1. ``runtime`` — WHERE the agent executes. Options:
       - ``chat-local``: plain LLM API calls from the daemon process (no tools).
       - ``sdk-local``: the provider's own agent SDK, in-process.
       - ``cli-local``: a ``claude``/``hermes``/``gemini`` CLI subprocess on the host.
       - ``cli-docker``: the same CLI inside a per-agent Docker container.
       - ``cli-sandbox``: reserved for a future sandboxed host runtime.

  2. ``provider`` — WHO serves the model. Options: ``anthropic`` /
     ``openai`` / ``google``.

  3. ``harness`` — WHAT agent engine runs inside the runtime (CLI
     kinds only — ``chat-local`` / ``sdk-local`` ignore this field
     since the SDK itself IS the harness). Options: ``claude-code``
     / ``hermes`` / ``gemini-cli``.

Not every combination is valid. Some harnesses are tightly bound to
a specific provider (``claude-code`` → anthropic, ``gemini-cli`` →
google); ``hermes`` is multi-provider. This module is the single
source of truth that encodes those constraints so the same rules
are enforced at agent-load time AND at ``puffoagent agent runtime``
flag-parse time.

Legacy kind names from pre-0.7.0 (``chat-only``, ``sdk``) are
accepted on load with a one-time WARNING and auto-migrated to their
new spellings. The migration shim will stay in 0.7.x and be removed
in 0.8.
"""

from __future__ import annotations

import logging
from typing import NamedTuple


logger = logging.getLogger(__name__)


# ── Enumerations ──────────────────────────────────────────────────────────────

RUNTIME_CHAT_LOCAL  = "chat-local"
RUNTIME_SDK_LOCAL   = "sdk-local"
RUNTIME_CLI_LOCAL   = "cli-local"
RUNTIME_CLI_DOCKER  = "cli-docker"
RUNTIME_CLI_SANDBOX = "cli-sandbox"  # reserved for a future release

VALID_RUNTIMES: frozenset[str] = frozenset({
    RUNTIME_CHAT_LOCAL,
    RUNTIME_SDK_LOCAL,
    RUNTIME_CLI_LOCAL,
    RUNTIME_CLI_DOCKER,
})

RESERVED_RUNTIMES: frozenset[str] = frozenset({
    RUNTIME_CLI_SANDBOX,
})


PROVIDER_ANTHROPIC = "anthropic"
PROVIDER_OPENAI    = "openai"
PROVIDER_GOOGLE    = "google"

VALID_PROVIDERS: frozenset[str] = frozenset({
    PROVIDER_ANTHROPIC,
    PROVIDER_OPENAI,
    PROVIDER_GOOGLE,
})


HARNESS_CLAUDE_CODE = "claude-code"
HARNESS_HERMES      = "hermes"
HARNESS_GEMINI_CLI  = "gemini-cli"

VALID_HARNESSES: frozenset[str] = frozenset({
    HARNESS_CLAUDE_CODE,
    HARNESS_HERMES,
    HARNESS_GEMINI_CLI,
})


# ── Constraints ───────────────────────────────────────────────────────────────

# Harness → providers it supports. A harness not in this map is
# unknown; a harness with an empty set is broken (should never
# happen in practice).
HARNESS_PROVIDERS: dict[str, frozenset[str]] = {
    HARNESS_CLAUDE_CODE: frozenset({PROVIDER_ANTHROPIC}),
    HARNESS_HERMES:      frozenset({PROVIDER_ANTHROPIC, PROVIDER_OPENAI}),
    HARNESS_GEMINI_CLI:  frozenset({PROVIDER_GOOGLE}),
}


# Runtimes where the ``harness`` field is meaningful. For the other
# runtimes the harness field is silently ignored (chat-local has no
# agent loop; sdk-local's loop is baked into the SDK).
_HARNESS_BEARING_RUNTIMES: frozenset[str] = frozenset({
    RUNTIME_CLI_LOCAL,
    RUNTIME_CLI_DOCKER,
})


def harness_applies(runtime: str) -> bool:
    """True when the runtime takes a ``harness`` field. For
    ``chat-local`` / ``sdk-local`` the agent engine is implicit and
    the field is ignored.
    """
    return runtime in _HARNESS_BEARING_RUNTIMES


# ── Default provider / harness per runtime ───────────────────────────────────

# When an agent.yml doesn't specify ``provider``, the daemon picks a
# default that keeps existing (pre-0.7.0) behaviour. For CLI + SDK
# runtimes that's Anthropic — the previous claude-code-only world.
# For ``chat-local`` we also default to Anthropic which matches
# ``DaemonConfig.default_provider``.
DEFAULT_PROVIDER_FOR_RUNTIME: dict[str, str] = {
    RUNTIME_CHAT_LOCAL: PROVIDER_ANTHROPIC,
    RUNTIME_SDK_LOCAL:  PROVIDER_ANTHROPIC,
    RUNTIME_CLI_LOCAL:  PROVIDER_ANTHROPIC,
    RUNTIME_CLI_DOCKER: PROVIDER_ANTHROPIC,
}

DEFAULT_HARNESS_FOR_PROVIDER: dict[str, str] = {
    PROVIDER_ANTHROPIC: HARNESS_CLAUDE_CODE,
    PROVIDER_OPENAI:    HARNESS_HERMES,
    PROVIDER_GOOGLE:    HARNESS_GEMINI_CLI,
}


# ── Legacy-name migration ─────────────────────────────────────────────────────

# Pre-0.7.0 ``runtime.kind`` values. The shim keeps existing
# agent.yml files working after an upgrade; a WARNING fires once per
# agent per daemon start.
_LEGACY_KIND_MIGRATIONS: dict[str, str] = {
    "chat-only": RUNTIME_CHAT_LOCAL,
    "sdk":       RUNTIME_SDK_LOCAL,
}


def migrate_legacy_kind(raw_kind: str, agent_id: str = "") -> str:
    """Translate a pre-0.7.0 ``kind`` value to its current spelling,
    logging a one-line WARNING if a migration actually happened.

    Returns the raw input unchanged when it's already current or
    when it's one we don't recognise (validation downstream will
    raise a clean error).
    """
    if raw_kind in _LEGACY_KIND_MIGRATIONS:
        new = _LEGACY_KIND_MIGRATIONS[raw_kind]
        logger.warning(
            "agent %s: runtime.kind %r is deprecated, use %r. "
            "auto-migrated for this run; please update agent.yml.",
            agent_id or "(?)", raw_kind, new,
        )
        return new
    return raw_kind


# ── Validation ────────────────────────────────────────────────────────────────


class ValidationResult(NamedTuple):
    ok: bool
    error: str  # empty when ok


def validate_triple(
    runtime: str, provider: str, harness: str,
) -> ValidationResult:
    """Check a (runtime, provider, harness) triple against the
    supported matrix. Empty ``provider`` / ``harness`` mean "use the
    default" and are accepted — callers resolve defaults separately.

    Returns a ``ValidationResult`` with a human-readable error
    describing which field is wrong and what would be valid.
    """
    if runtime in RESERVED_RUNTIMES:
        return ValidationResult(False, (
            f"runtime kind {runtime!r} is reserved for a future release "
            "and not yet implemented"
        ))
    if runtime not in VALID_RUNTIMES:
        return ValidationResult(False, (
            f"unknown runtime kind {runtime!r} "
            f"(valid: {', '.join(sorted(VALID_RUNTIMES))})"
        ))

    if provider and provider not in VALID_PROVIDERS:
        return ValidationResult(False, (
            f"unknown provider {provider!r} "
            f"(valid: {', '.join(sorted(VALID_PROVIDERS))})"
        ))

    if not harness_applies(runtime):
        # harness field is ignored here; if someone set a value
        # that's fine — we don't reject, just won't use it.
        return ValidationResult(True, "")

    if not harness:
        # CLI runtimes without an explicit harness default to
        # claude-code (historical behaviour). Validation is a no-op.
        return ValidationResult(True, "")

    if harness not in VALID_HARNESSES:
        return ValidationResult(False, (
            f"unknown harness {harness!r} "
            f"(valid: {', '.join(sorted(VALID_HARNESSES))})"
        ))

    if provider:
        supported = HARNESS_PROVIDERS.get(harness, frozenset())
        if provider not in supported:
            return ValidationResult(False, (
                f"harness {harness!r} does not support provider "
                f"{provider!r} (supported: {', '.join(sorted(supported)) or '(none)'})"
            ))

    return ValidationResult(True, "")


def resolve_effective_provider(runtime: str, provider: str) -> str:
    """Fill in the runtime-specific default provider when the
    agent.yml field is empty. Returns the input unchanged when set.
    """
    if provider:
        return provider
    return DEFAULT_PROVIDER_FOR_RUNTIME.get(runtime, PROVIDER_ANTHROPIC)


def resolve_effective_harness(runtime: str, provider: str, harness: str) -> str:
    """Fill in a sensible default harness for CLI runtimes when the
    field is empty. Returns the input unchanged when already set, or
    empty string for runtimes where the field doesn't apply.
    """
    if not harness_applies(runtime):
        return ""
    if harness:
        return harness
    provider = resolve_effective_provider(runtime, provider)
    return DEFAULT_HARNESS_FOR_PROVIDER.get(provider, HARNESS_CLAUDE_CODE)


__all__ = [
    # runtime constants
    "RUNTIME_CHAT_LOCAL", "RUNTIME_SDK_LOCAL",
    "RUNTIME_CLI_LOCAL", "RUNTIME_CLI_DOCKER", "RUNTIME_CLI_SANDBOX",
    # provider constants
    "PROVIDER_ANTHROPIC", "PROVIDER_OPENAI", "PROVIDER_GOOGLE",
    # harness constants
    "HARNESS_CLAUDE_CODE", "HARNESS_HERMES", "HARNESS_GEMINI_CLI",
    # sets
    "VALID_RUNTIMES", "RESERVED_RUNTIMES",
    "VALID_PROVIDERS", "VALID_HARNESSES",
    "HARNESS_PROVIDERS",
    # helpers
    "harness_applies",
    "migrate_legacy_kind",
    "validate_triple",
    "ValidationResult",
    "resolve_effective_provider",
    "resolve_effective_harness",
]
