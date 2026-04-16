"""One-shot migration from the pre-portal single-agent layout.

The daemon used to be invoked as ``python main.py`` from inside the
``puffoagent/`` repo directory, with everything in a single ``config.yml``
next to ``main.py`` and profile/memory alongside. The portal layout is a
per-user home directory with many agents. On first ``puffoagent start``,
we migrate that legacy layout into ``~/.puffoagent/agents/default/`` so
an existing user's bot keeps working without manual intervention.

The migration is idempotent — if ``~/.puffoagent/agents`` already has
entries, we do nothing.
"""

from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path

import yaml

from .state import (
    AgentConfig,
    AgentProviderOverride,
    DaemonConfig,
    MattermostConfig,
    ProviderConfig,
    TriggerRules,
    agent_dir,
    agents_dir,
    daemon_yml_path,
    home_dir,
    is_valid_agent_id,
)

logger = logging.getLogger(__name__)

# The legacy single-agent layout lives in the repo's puffoagent/ directory
# next to the old config.yml. After the src/-layout reorg this file is at
# puffoagent/src/puffoagent/portal/migrate.py, so we walk up four parents
# to reach the outer puffoagent/ dir. When the package is installed from a
# non-editable wheel that path won't contain a config.yml, so migration is
# simply skipped.
LEGACY_REPO_ROOT = Path(__file__).resolve().parents[3]


def migrate_legacy_repo_layout_if_needed() -> None:
    agents_root = agents_dir()
    if agents_root.exists() and any(agents_root.iterdir()):
        return  # user already has portal-style agents

    legacy_config = LEGACY_REPO_ROOT / "config.yml"
    if not legacy_config.exists():
        return  # nothing to migrate

    try:
        with legacy_config.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except Exception as exc:
        logger.warning("legacy config.yml exists but could not be read: %s", exc)
        return

    mm = raw.get("mattermost") or {}
    ai = raw.get("ai") or {}
    agent_section = raw.get("agent") or {}

    agent_id = "default"
    if not is_valid_agent_id(agent_id):
        return
    legacy_dir = agent_dir(agent_id)
    legacy_dir.mkdir(parents=True, exist_ok=True)

    # daemon.yml — seeded from the legacy AI provider block. Skills dir
    # defaults to the legacy shared skills directory so the migrated
    # agent keeps seeing them.
    home_dir().mkdir(parents=True, exist_ok=True)
    if not daemon_yml_path().exists():
        default_provider = ai.get("default_provider", "anthropic")
        daemon_cfg = DaemonConfig(default_provider=default_provider)
        anth = ai.get("anthropic") or {}
        oai = ai.get("openai") or {}
        daemon_cfg.anthropic = ProviderConfig(
            api_key=anth.get("api_key", ""),
            model=anth.get("model", "claude-sonnet-4-6"),
        )
        daemon_cfg.openai = ProviderConfig(
            api_key=oai.get("api_key", ""),
            model=oai.get("model", "gpt-4o"),
        )
        legacy_skills = LEGACY_REPO_ROOT / "skills"
        if legacy_skills.exists():
            daemon_cfg.skills_dir = str(legacy_skills)
        daemon_cfg.save()
        logger.info("migration: wrote daemon.yml from legacy config.yml")

    # Copy the legacy profile next to the agent. The legacy config
    # references a path like "agents/default.md" relative to the repo.
    legacy_profile_rel = agent_section.get("profile", "agents/default.md")
    legacy_profile_path = LEGACY_REPO_ROOT / legacy_profile_rel
    dest_profile = legacy_dir / "profile.md"
    if legacy_profile_path.exists() and not dest_profile.exists():
        shutil.copy2(legacy_profile_path, dest_profile)

    # Copy the legacy memory directory so token usage + memory files
    # survive the migration.
    legacy_memory_rel = agent_section.get("memory_dir", "memory/")
    legacy_memory_path = LEGACY_REPO_ROOT / legacy_memory_rel
    dest_memory = legacy_dir / "memory"
    if legacy_memory_path.exists() and not dest_memory.exists():
        shutil.copytree(legacy_memory_path, dest_memory)
    else:
        dest_memory.mkdir(parents=True, exist_ok=True)

    agent_cfg = AgentConfig(
        id=agent_id,
        state="running",
        display_name="Default agent (migrated)",
        mattermost=MattermostConfig(
            url=mm.get("url", "http://localhost:8065"),
            bot_token=mm.get("bot_token", ""),
            team_name=mm.get("team_name", ""),
        ),
        ai=AgentProviderOverride(),  # inherit daemon defaults
        profile="profile.md",
        memory_dir="memory",
        triggers=TriggerRules(
            on_mention=bool(agent_section.get("trigger_on_mention", True)),
            on_dm=bool(agent_section.get("trigger_on_dm", True)),
        ),
        created_at=int(time.time()),
    )
    agent_cfg.save()
    logger.info(
        "migration: created agent %s at %s from legacy config.yml",
        agent_id, legacy_dir,
    )
