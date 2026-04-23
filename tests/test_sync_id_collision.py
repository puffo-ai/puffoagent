"""Tests for the collision-avoiding ``_derive_agent_id`` in
``portal/sync.py``.

Contract (no migration, first-created agent keeps the plain name):

  1. ASCII slug of display_name is the default derived id.
  2. CJK-only (empty slug) names fall back to user_id — no collision
     possible since user_ids are globally unique.
  3. If the slug collides with another agent already assigned this
     sync pass, the NEW one gets ``<base>-<user_id_prefix>`` and the
     first one keeps the plain name. Ordering is anchored on
     ``create_at`` (oldest wins).
  4. An agent re-syncing (local dir already owned by its user_id)
     keeps its plain name even if a same-name agent exists on the
     server — the re-sync is the same agent, not a collision.
  5. The sort + iterate loop is caller-side (``sync_from_server``);
     these tests exercise ``_derive_agent_id`` directly with the
     ``taken_ids`` / ``local_owned_ids`` params the caller builds.
"""

from __future__ import annotations

from puffoagent.portal.sync import (
    _ascii_slug,
    _derive_agent_id,
)


# ── _ascii_slug ──────────────────────────────────────────────────────────────


def test_ascii_slug_keeps_alnum_and_dash_underscore():
    assert _ascii_slug("Hello World") == "hello-world"
    assert _ascii_slug("my_agent-01") == "my_agent-01"


def test_ascii_slug_drops_non_ascii():
    assert _ascii_slug("d2d2迷你") == "d2d2"
    assert _ascii_slug("d2d2留声机") == "d2d2"


def test_ascii_slug_empty_for_cjk_only():
    assert _ascii_slug("张三") == ""
    assert _ascii_slug("欢迎回来") == ""


def test_ascii_slug_strips_leading_trailing_separators():
    assert _ascii_slug("  --foo--  ") == "foo"
    assert _ascii_slug("__bar__") == "bar"


# ── _derive_agent_id — first-wins, suffix-on-conflict ────────────────────────


def test_derive_id_uses_plain_slug_when_no_conflict():
    remote = {"display_name": "d2d2迷你", "user_id": "abc123def456"}
    assert _derive_agent_id(remote) == "d2d2"


def test_derive_id_falls_back_to_user_id_for_cjk_only_names():
    remote = {"display_name": "张三", "user_id": "abc123def456abcdef"}
    # user_id truncated to 26 chars (the id-validation cap).
    assert _derive_agent_id(remote) == "abc123def456abcdef"


def test_derive_id_oldest_wins_plain_name_newer_gets_suffix():
    """First-created agent claims the plain slug; subsequent
    collisions get ``<base>-<user_id_prefix>``."""
    first = {"display_name": "d2d2迷你", "user_id": "aaaaaaa1111"}
    second = {"display_name": "d2d2留声机", "user_id": "bbbbbbb2222"}

    # Caller iterates oldest-first and builds ``taken_ids`` as it goes.
    taken: set[str] = set()
    first_id = _derive_agent_id(first, taken_ids=taken)
    taken.add(first_id)
    second_id = _derive_agent_id(second, taken_ids=taken)

    assert first_id == "d2d2"
    assert second_id == "d2d2-bbbbbbb"
    assert first_id != second_id


def test_derive_id_three_way_collision_each_gets_its_own_suffix():
    remotes = [
        {"display_name": "d2d2迷你", "user_id": "aaaaaaa1111"},
        {"display_name": "d2d2留声机", "user_id": "bbbbbbb2222"},
        {"display_name": "d2d2相册", "user_id": "ccccccc3333"},
    ]
    taken: set[str] = set()
    ids = []
    for r in remotes:
        aid = _derive_agent_id(r, taken_ids=taken)
        ids.append(aid)
        taken.add(aid)

    assert ids == ["d2d2", "d2d2-bbbbbbb", "d2d2-ccccccc"]


def test_derive_id_resync_preserves_plain_name_for_same_owner():
    """When the local dir already belongs to this same bot
    (``local_owned_ids[base] == user_id``), re-sync must return the
    plain slug — it's the same agent, not a collision."""
    remote = {"display_name": "d2d2迷你", "user_id": "aaaaaaa1111"}
    # Local dir 'd2d2' is owned by user_id 'aaaaaaa1111' (same agent).
    # But `taken_ids` hasn't seen it yet in this pass.
    derived = _derive_agent_id(
        remote,
        taken_ids=set(),
        local_owned_ids={"d2d2": "aaaaaaa1111"},
    )
    assert derived == "d2d2"


def test_derive_id_new_agent_with_same_slug_as_existing_local_gets_suffix():
    """If the local dir named 'd2d2' belongs to a DIFFERENT user_id,
    the new agent must not steal the plain name — it gets a suffix
    so the older local agent's dir stays intact."""
    remote = {"display_name": "d2d2留声机", "user_id": "bbbbbbb2222"}
    derived = _derive_agent_id(
        remote,
        taken_ids=set(),
        local_owned_ids={"d2d2": "aaaaaaa1111"},  # owned by a different user
    )
    assert derived == "d2d2-bbbbbbb"


def test_derive_id_base_name_truncated_to_leave_room_for_suffix():
    """``is_valid_agent_id`` caps at 64 chars. When the base is near
    the cap AND a suffix is needed, the base gets truncated further
    to keep the total under 64."""
    long_name = "a" * 80
    first = {"display_name": long_name, "user_id": "aaaaaaa1111"}
    second = {"display_name": long_name, "user_id": "bbbbbbb2222"}

    taken: set[str] = set()
    first_id = _derive_agent_id(first, taken_ids=taken)
    taken.add(first_id)
    second_id = _derive_agent_id(second, taken_ids=taken)

    # Even the plain base is truncated preemptively to 56 so the
    # suffix always fits — keeps first and second pass consistent.
    assert len(first_id) <= 64
    assert len(second_id) <= 64
    assert second_id.endswith("-bbbbbbb")
    assert second_id != first_id


def test_derive_id_stable_across_multiple_sync_passes():
    """Same inputs in the same order always yield the same outputs —
    no random salt, no timestamp dependence."""
    remotes = [
        {"display_name": "d2d2迷你", "user_id": "aaa"},
        {"display_name": "d2d2留声机", "user_id": "bbb"},
    ]
    for _ in range(3):
        taken: set[str] = set()
        ids = []
        for r in remotes:
            aid = _derive_agent_id(r, taken_ids=taken)
            ids.append(aid)
            taken.add(aid)
        assert ids == ["d2d2", "d2d2-bbb"]


def test_derive_id_profile_name_as_fallback_display_source():
    """Older server records may use ``profile_name`` instead of
    ``display_name``. Derivation accepts either."""
    remote = {"profile_name": "HelpBot", "user_id": "xyz"}
    assert _derive_agent_id(remote) == "helpbot"


def test_derive_id_empty_remote_falls_back_to_user_id():
    remote = {"user_id": "abc123"}
    assert _derive_agent_id(remote) == "abc123"
