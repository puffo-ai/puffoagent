"""Shared MCP config builders.

Three adapter kinds register the same puffo_tools MCP server, but via
slightly different transports. This module centralises the
server-spec construction so every adapter agrees on env var names,
argument order, and tool-name prefixes.

Tool names exposed as ``mcp__puffo__<tool>`` when invoked from
claude-code. Callers that need to allowlist tools should reference
that form.
"""

from __future__ import annotations

import inspect
import json
import os
import sys
from pathlib import Path


MCP_SERVER_NAME = "puffo"

# Tool names exposed by the MCP server, prefixed the way claude-code
# expects them in allowlists / permission-prompt-tool arguments.
PUFFO_TOOL_NAMES = (
    "send_message",
    "upload_file",
    "list_channels",
    "list_channel_members",
    "get_channel_history",
    "fetch_channel_files",
    "get_post",
    "get_user_info",
    "reload_system_prompt",
    "install_skill",
    "uninstall_skill",
    "list_skills",
    "install_mcp_server",
    "uninstall_mcp_server",
    "list_mcp_servers",
    "refresh",
    "approve_permission",
)
PUFFO_TOOL_FQNS = tuple(f"mcp__{MCP_SERVER_NAME}__{t}" for t in PUFFO_TOOL_NAMES)

# The claude-code --permission-prompt-tool flag takes the fully-
# qualified MCP tool name of the callback.
PERMISSION_PROMPT_TOOL = f"mcp__{MCP_SERVER_NAME}__approve_permission"


def mcp_env(
    *,
    agent_id: str,
    url: str,
    token: str,
    workspace: str,
    team: str = "",
    owner_username: str = "",
    permission_timeout_seconds: float = 300.0,
    runtime_kind: str = "",
    harness: str = "",
) -> dict[str, str]:
    """Env dict to pass to the MCP subprocess. Puts secrets in env
    rather than argv so they don't appear in process listings.

    ``runtime_kind`` propagates the adapter kind (``cli-local`` /
    ``cli-docker`` / ``sdk``) so the MCP server can make
    runtime-aware decisions — e.g., ``install_mcp_server`` rejects
    host-local command paths inside ``cli-docker`` (they won't
    resolve in the container) but accepts them under ``cli-local``
    where the agent runs on the host.

    ``harness`` propagates the agent engine (``claude-code`` /
    ``hermes`` / ...) so tools that only make sense under Claude
    Code (install_skill, refresh, project-scope .mcp.json writers)
    can short-circuit with a clear message under other harnesses.
    """
    env: dict[str, str] = {
        "PUFFO_AGENT_ID": agent_id,
        "PUFFO_URL": url,
        "PUFFO_BOT_TOKEN": token,
        "PUFFO_WORKSPACE": workspace,
        "PUFFO_PERMISSION_TIMEOUT": str(permission_timeout_seconds),
    }
    if team:
        env["PUFFO_TEAM"] = team
    if owner_username:
        env["PUFFO_OWNER_USERNAME"] = owner_username
    if runtime_kind:
        env["PUFFO_RUNTIME_KIND"] = runtime_kind
    if harness:
        env["PUFFO_HARNESS"] = harness
    return env


def stdio_sdk_config(
    *,
    python: str,
    agent_id: str,
    url: str,
    token: str,
    workspace: str,
    team: str = "",
    owner_username: str = "",
    permission_timeout_seconds: float = 300.0,
) -> dict:
    """Return the ``mcp_servers`` config dict for the claude-agent-sdk
    adapter. Passed to ``ClaudeAgentOptions.mcp_servers``.
    """
    return {
        MCP_SERVER_NAME: {
            "type": "stdio",
            "command": python,
            "args": ["-m", "puffoagent.mcp.puffo_tools"],
            "env": mcp_env(
                agent_id=agent_id, url=url, token=token, workspace=workspace,
                team=team, owner_username=owner_username,
                permission_timeout_seconds=permission_timeout_seconds,
                runtime_kind="sdk",
            ),
        }
    }


def cli_mcp_config_doc(
    *,
    command: str,
    args: list[str],
    env: dict[str, str],
) -> dict:
    """Build the document you'd write to an ``mcp-config.json`` file
    for claude-code's ``--mcp-config`` flag. Uses the top-level
    ``mcpServers`` key and kebab-style stdio schema the CLI expects."""
    return {
        "mcpServers": {
            MCP_SERVER_NAME: {
                "type": "stdio",
                "command": command,
                "args": list(args),
                "env": dict(env),
            }
        }
    }


def write_cli_mcp_config(
    dest: Path,
    *,
    command: str,
    args: list[str],
    env: dict[str, str],
) -> Path:
    """Serialise the CLI MCP config to ``dest``. Creates parent dirs.
    Returns the written path."""
    doc = cli_mcp_config_doc(command=command, args=args, env=env)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return dest


def export_mcp_script(dest_dir: Path) -> Path:
    """Copy the puffo_tools.py source to ``dest_dir/puffo_tools.py``
    so cli-docker can bind-mount the script into the container
    without caring about how the user installed puffoagent. Returns
    the written path.

    We read the source via ``inspect.getsource`` rather than importing
    ``puffoagent.mcp.puffo_tools.__file__`` because the latter doesn't
    cooperate with all install layouts (eg. zipimport). The module is
    deliberately self-contained so a plain ``python3 puffo_tools.py``
    works with only ``mcp`` and ``aiohttp`` on PYTHONPATH.
    """
    from . import puffo_tools
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "puffo_tools.py"
    dest.write_text(inspect.getsource(puffo_tools), encoding="utf-8")
    return dest


def default_python_executable() -> str:
    """Path to the Python interpreter the daemon itself is running
    under — used for SDK and cli-local, where MCP runs on the host
    in the same interpreter tree as puffoagent.
    """
    return sys.executable or "python3"
