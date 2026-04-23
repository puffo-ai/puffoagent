"""Unit tests for ``portal/runtime_matrix.py``.

Covers:

  1. Legacy kind migration (``chat-only`` → ``chat-local``, ``sdk``
     → ``sdk-local``) emits a WARNING and returns the new spelling.
  2. ``validate_triple`` accepts every documented valid combo and
     rejects every invalid one with a clear error.
  3. ``cli-sandbox`` is recognised as reserved (distinct error from
     an unknown kind) so operators trying to use the future runtime
     hit an actionable message.
  4. Default-resolver helpers fill in empty fields consistently.
"""

from __future__ import annotations

import logging

import pytest

from puffoagent.portal.runtime_matrix import (
    DEFAULT_HARNESS_FOR_PROVIDER,
    DEFAULT_PROVIDER_FOR_RUNTIME,
    HARNESS_CLAUDE_CODE,
    HARNESS_GEMINI_CLI,
    HARNESS_HERMES,
    HARNESS_PROVIDERS,
    PROVIDER_ANTHROPIC,
    PROVIDER_GOOGLE,
    PROVIDER_OPENAI,
    RESERVED_RUNTIMES,
    RUNTIME_CHAT_LOCAL,
    RUNTIME_CLI_DOCKER,
    RUNTIME_CLI_LOCAL,
    RUNTIME_CLI_SANDBOX,
    RUNTIME_SDK_LOCAL,
    VALID_HARNESSES,
    VALID_PROVIDERS,
    VALID_RUNTIMES,
    harness_applies,
    migrate_legacy_kind,
    resolve_effective_harness,
    resolve_effective_provider,
    validate_triple,
)


# ── migrate_legacy_kind ──────────────────────────────────────────────────────


def test_migrate_chat_only_to_chat_local(caplog):
    with caplog.at_level(logging.WARNING, logger="puffoagent.portal.runtime_matrix"):
        assert migrate_legacy_kind("chat-only", agent_id="alice") == RUNTIME_CHAT_LOCAL
    assert any(
        "chat-only" in r.message and "chat-local" in r.message and "alice" in r.message
        for r in caplog.records
    )


def test_migrate_sdk_to_sdk_local(caplog):
    with caplog.at_level(logging.WARNING, logger="puffoagent.portal.runtime_matrix"):
        assert migrate_legacy_kind("sdk", agent_id="bob") == RUNTIME_SDK_LOCAL
    assert any("sdk" in r.message and "sdk-local" in r.message for r in caplog.records)


def test_migrate_modern_names_pass_through_silently(caplog):
    """Current-era names shouldn't trigger a WARNING — the shim
    only fires when an actual migration happened."""
    for name in (RUNTIME_CHAT_LOCAL, RUNTIME_SDK_LOCAL,
                 RUNTIME_CLI_LOCAL, RUNTIME_CLI_DOCKER):
        with caplog.at_level(logging.WARNING, logger="puffoagent.portal.runtime_matrix"):
            assert migrate_legacy_kind(name, agent_id="c") == name
    assert not caplog.records, "no WARNING expected for already-current kinds"


def test_migrate_unknown_kind_passes_through_for_validator_to_reject():
    """Garbage input is returned unchanged so downstream
    ``validate_triple`` can produce the canonical error."""
    assert migrate_legacy_kind("not-a-kind") == "not-a-kind"


# ── validate_triple — positive cases ─────────────────────────────────────────


@pytest.mark.parametrize("runtime,provider,harness", [
    # chat-local: every declared provider is valid; harness ignored
    (RUNTIME_CHAT_LOCAL, PROVIDER_ANTHROPIC, ""),
    (RUNTIME_CHAT_LOCAL, PROVIDER_OPENAI, ""),
    (RUNTIME_CHAT_LOCAL, PROVIDER_GOOGLE, ""),
    (RUNTIME_CHAT_LOCAL, "", ""),                   # empty provider = use default

    # sdk-local: same — harness not required
    (RUNTIME_SDK_LOCAL, PROVIDER_ANTHROPIC, ""),
    (RUNTIME_SDK_LOCAL, PROVIDER_OPENAI, ""),
    (RUNTIME_SDK_LOCAL, PROVIDER_GOOGLE, ""),

    # cli-local / cli-docker: harness matters
    (RUNTIME_CLI_LOCAL,  PROVIDER_ANTHROPIC, HARNESS_CLAUDE_CODE),
    (RUNTIME_CLI_LOCAL,  PROVIDER_ANTHROPIC, HARNESS_HERMES),
    (RUNTIME_CLI_LOCAL,  PROVIDER_OPENAI,    HARNESS_HERMES),
    (RUNTIME_CLI_LOCAL,  PROVIDER_GOOGLE,    HARNESS_GEMINI_CLI),
    (RUNTIME_CLI_DOCKER, PROVIDER_ANTHROPIC, HARNESS_CLAUDE_CODE),
    (RUNTIME_CLI_DOCKER, PROVIDER_GOOGLE,    HARNESS_GEMINI_CLI),

    # Empty harness on CLI kinds falls back to the runtime default
    (RUNTIME_CLI_LOCAL,  PROVIDER_ANTHROPIC, ""),
    (RUNTIME_CLI_DOCKER, "",                 ""),
])
def test_validate_triple_accepts_valid_combos(runtime, provider, harness):
    result = validate_triple(runtime, provider, harness)
    assert result.ok, f"expected ({runtime}, {provider}, {harness}) to validate; got: {result.error}"
    assert result.error == ""


# ── validate_triple — negative cases ─────────────────────────────────────────


def test_validate_triple_rejects_reserved_cli_sandbox():
    result = validate_triple(RUNTIME_CLI_SANDBOX, PROVIDER_ANTHROPIC, HARNESS_CLAUDE_CODE)
    assert not result.ok
    assert "reserved" in result.error.lower() or "not yet implemented" in result.error.lower()


def test_validate_triple_rejects_unknown_runtime():
    result = validate_triple("quantum-runtime", PROVIDER_ANTHROPIC, HARNESS_CLAUDE_CODE)
    assert not result.ok
    assert "unknown runtime" in result.error.lower()
    # Error message lists valid options so the user knows what to pick.
    for valid in VALID_RUNTIMES:
        assert valid in result.error


def test_validate_triple_rejects_unknown_provider():
    result = validate_triple(RUNTIME_CHAT_LOCAL, "cohere", "")
    assert not result.ok
    assert "unknown provider" in result.error.lower()


def test_validate_triple_rejects_unknown_harness_on_cli_runtime():
    result = validate_triple(RUNTIME_CLI_LOCAL, PROVIDER_ANTHROPIC, "lanchain")
    assert not result.ok
    assert "unknown harness" in result.error.lower()


def test_validate_triple_rejects_claude_code_with_google():
    """claude-code harness is anthropic-only — rejecting at load
    time is the whole point of this validator."""
    result = validate_triple(RUNTIME_CLI_DOCKER, PROVIDER_GOOGLE, HARNESS_CLAUDE_CODE)
    assert not result.ok
    assert "claude-code" in result.error
    assert "google" in result.error


def test_validate_triple_rejects_gemini_cli_with_anthropic():
    result = validate_triple(RUNTIME_CLI_DOCKER, PROVIDER_ANTHROPIC, HARNESS_GEMINI_CLI)
    assert not result.ok
    assert "gemini-cli" in result.error
    assert "anthropic" in result.error


def test_validate_triple_rejects_hermes_with_google():
    """Hermes upstream doesn't support Google; matrix blocks it
    until upstream changes."""
    result = validate_triple(RUNTIME_CLI_DOCKER, PROVIDER_GOOGLE, HARNESS_HERMES)
    assert not result.ok
    assert "hermes" in result.error


def test_validate_triple_ignores_harness_for_non_cli_runtimes():
    """For chat-local / sdk-local the harness field is silently
    ignored — the agent engine is implicit. Even nonsense values
    don't trigger a failure here."""
    for runtime in (RUNTIME_CHAT_LOCAL, RUNTIME_SDK_LOCAL):
        result = validate_triple(runtime, PROVIDER_ANTHROPIC, "lanchain")
        assert result.ok, f"{runtime} should ignore harness field"


# ── harness_applies ──────────────────────────────────────────────────────────


def test_harness_applies_only_for_cli_runtimes():
    assert harness_applies(RUNTIME_CLI_LOCAL) is True
    assert harness_applies(RUNTIME_CLI_DOCKER) is True
    assert harness_applies(RUNTIME_CHAT_LOCAL) is False
    assert harness_applies(RUNTIME_SDK_LOCAL) is False


# ── default resolvers ────────────────────────────────────────────────────────


def test_resolve_effective_provider_fills_default_per_runtime():
    for runtime in VALID_RUNTIMES:
        assert resolve_effective_provider(runtime, "") == DEFAULT_PROVIDER_FOR_RUNTIME[runtime]


def test_resolve_effective_provider_preserves_explicit_value():
    assert resolve_effective_provider(RUNTIME_CLI_DOCKER, PROVIDER_OPENAI) == PROVIDER_OPENAI


def test_resolve_effective_harness_empty_for_non_cli_runtimes():
    """chat-local and sdk-local never carry a harness — the
    resolver returns empty regardless of provider."""
    assert resolve_effective_harness(RUNTIME_CHAT_LOCAL, PROVIDER_ANTHROPIC, "") == ""
    assert resolve_effective_harness(RUNTIME_SDK_LOCAL, PROVIDER_GOOGLE, "") == ""


def test_resolve_effective_harness_fills_cli_default():
    """Empty harness on a CLI runtime picks the provider-natural
    default (anthropic → claude-code, google → gemini-cli)."""
    assert resolve_effective_harness(
        RUNTIME_CLI_LOCAL, PROVIDER_ANTHROPIC, "",
    ) == HARNESS_CLAUDE_CODE
    assert resolve_effective_harness(
        RUNTIME_CLI_DOCKER, PROVIDER_GOOGLE, "",
    ) == HARNESS_GEMINI_CLI


def test_resolve_effective_harness_preserves_explicit_value():
    assert resolve_effective_harness(
        RUNTIME_CLI_DOCKER, PROVIDER_ANTHROPIC, HARNESS_HERMES,
    ) == HARNESS_HERMES


# ── Matrix invariants ────────────────────────────────────────────────────────


def test_every_valid_harness_declares_at_least_one_provider():
    """If a harness is in VALID_HARNESSES but HARNESS_PROVIDERS
    returns an empty set, the validator would reject every provider
    — broken by construction. Catch that here."""
    for h in VALID_HARNESSES:
        providers = HARNESS_PROVIDERS.get(h, frozenset())
        assert providers, f"harness {h!r} has no supported providers — broken matrix entry"
        for p in providers:
            assert p in VALID_PROVIDERS


def test_cli_sandbox_is_reserved_not_valid():
    """``cli-sandbox`` must sit in RESERVED_RUNTIMES (not VALID)
    so the validator surfaces the distinct 'not yet implemented'
    message rather than 'unknown runtime'."""
    assert RUNTIME_CLI_SANDBOX in RESERVED_RUNTIMES
    assert RUNTIME_CLI_SANDBOX not in VALID_RUNTIMES


def test_default_harness_for_each_provider_is_valid():
    """If the default-harness-for-provider map picks a harness
    that doesn't list the provider as supported, we'd recommend an
    invalid triple."""
    for provider, harness in DEFAULT_HARNESS_FOR_PROVIDER.items():
        assert provider in HARNESS_PROVIDERS[harness], (
            f"default harness {harness!r} for provider {provider!r} "
            "doesn't declare that provider as supported"
        )
