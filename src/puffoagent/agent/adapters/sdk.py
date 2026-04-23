"""claude-agent-sdk adapter.

Runs the agentic loop inside our process via ``claude-agent-sdk``'s
``query()`` function. Each turn is one ``query()`` call — the shell's
conversation log is embedded into the prompt so the SDK sees prior
turns as context. We do not keep a long-lived ``ClaudeSDKClient``
session; correctness across shell-side log truncation and worker
restarts is easier when every turn is stateless from the SDK's view.

The adapter:
  - translates ``TurnContext`` into ``ClaudeAgentOptions``
  - streams messages from ``query()`` back into a ``TurnResult``
  - gates tool calls against ``allowed_tools`` patterns via
    ``can_use_tool`` (see ``_gate`` below).

``claude-agent-sdk`` is an optional dependency. The import is deferred
so puffoagent can run other adapters without it installed.
"""

from __future__ import annotations

import fnmatch
import logging
from typing import Any

from ...mcp.config import (
    PUFFO_TOOL_FQNS,
    default_python_executable,
    stdio_sdk_config,
)
from .base import Adapter, TurnContext, TurnResult, format_history_as_prompt

logger = logging.getLogger(__name__)


class SDKAdapter(Adapter):
    def __init__(
        self,
        api_key: str,
        model: str,
        allowed_tools: list[str] | None = None,
        permission_mode: str | None = None,
        agent_id: str = "",
        mattermost_url: str = "",
        mattermost_token: str = "",
        workspace_dir: str = "",
        team: str = "",
        owner_username: str = "",
        max_turns: int = 10,
    ):
        try:
            from claude_agent_sdk import (
                query,
                ClaudeAgentOptions,
                AssistantMessage,
                TextBlock,
                ToolUseBlock,
                ResultMessage,
            )
        except ImportError as e:
            raise RuntimeError(
                "runtime kind 'sdk' requires the claude-agent-sdk package. "
                "install with `pip install claude-agent-sdk` or "
                "`pip install puffoagent[sdk]`"
            ) from e

        self._query = query
        self._Options = ClaudeAgentOptions
        self._AssistantMessage = AssistantMessage
        self._TextBlock = TextBlock
        self._ToolUseBlock = ToolUseBlock
        self._ResultMessage = ResultMessage

        self.api_key = api_key
        self.model = model
        self.patterns = list(allowed_tools or [])
        # Puffo MCP tools are baked-in capabilities the agent should
        # always have access to when configured — auto-allow them
        # without asking users to thread mcp__puffo__send_message
        # through their allowed_tools list.
        self.patterns.extend(PUFFO_TOOL_FQNS)
        self.permission_mode = permission_mode
        self.agent_id = agent_id
        self.mattermost_url = mattermost_url
        self.mattermost_token = mattermost_token
        self.workspace_dir = workspace_dir
        self.team = team
        self.owner_username = owner_username
        self.max_turns = max_turns

    async def run_turn(self, ctx: TurnContext) -> TurnResult:
        mcp_servers = {}
        if self.mattermost_url and self.mattermost_token and self.agent_id:
            mcp_servers = stdio_sdk_config(
                python=default_python_executable(),
                agent_id=self.agent_id,
                url=self.mattermost_url,
                token=self.mattermost_token,
                workspace=self.workspace_dir or ctx.workspace_dir or ".",
                team=self.team,
                owner_username=self.owner_username,
            )

        options = self._Options(
            system_prompt=ctx.system_prompt,
            cwd=ctx.workspace_dir or None,
            # Route every tool call through our gate so pattern globs
            # like "Bash(git *)" can match against tool input. If we
            # populated allowed_tools here the SDK would auto-approve
            # by bare name before can_use_tool ran.
            allowed_tools=[],
            can_use_tool=self._gate,
            permission_mode=self.permission_mode,
            model=self.model or None,
            env={"ANTHROPIC_API_KEY": self.api_key} if self.api_key else {},
            mcp_servers=mcp_servers,
            setting_sources=["project"],  # pick up .claude/CLAUDE.md under cwd
            max_turns=self.max_turns,
        )

        reply_parts: list[str] = []
        tool_calls = 0
        input_tokens = 0
        output_tokens = 0
        # See cli_session.py — same contract, same double-post story.
        # tool_names is debug; send_message_targets is what the shell
        # actually uses to decide suppression.
        tool_names_used: list[str] = []
        send_message_targets: list[dict] = []

        # The SDK requires streaming-mode input (AsyncIterable of message
        # dicts) whenever can_use_tool is set — a plain string prompt
        # raises "can_use_tool callback requires streaming mode".
        async for msg in self._query(
            prompt=_prompt_stream(format_history_as_prompt(ctx.messages)),
            options=options,
        ):
            if isinstance(msg, self._AssistantMessage):
                for block in msg.content:
                    if isinstance(block, self._TextBlock):
                        reply_parts.append(block.text)
                    elif isinstance(block, self._ToolUseBlock):
                        tool_calls += 1
                        tool_names_used.append(block.name)
                        if block.name == "mcp__puffo__send_message":
                            tool_input = block.input or {}
                            send_message_targets.append({
                                "channel": str(tool_input.get("channel", "")),
                                "root_id": str(tool_input.get("root_id", "")),
                            })
                        if ctx.on_progress is not None:
                            try:
                                await ctx.on_progress(f"🔨 {block.name}")
                            except Exception as exc:
                                logger.debug("on_progress failed: %s", exc)
            elif isinstance(msg, self._ResultMessage):
                usage = msg.usage or {}
                input_tokens = int(usage.get("input_tokens", 0))
                output_tokens = int(usage.get("output_tokens", 0))

        return TurnResult(
            reply="\n".join(reply_parts).strip(),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            tool_calls=tool_calls,
            metadata={
                "tool_names": tool_names_used,
                "send_message_targets": send_message_targets,
            },
        )

    async def _gate(self, tool_name: str, tool_input: dict, context: Any) -> dict:
        """``can_use_tool`` callback. Allow if any configured pattern
        matches the tool name + input; otherwise deny with an
        informative message that surfaces to the agent.
        """
        for pat in self.patterns:
            if _pattern_matches(tool_name, tool_input, pat):
                return {"behavior": "allow", "updatedInput": tool_input}
        return {
            "behavior": "deny",
            "message": (
                f"tool {tool_name!r} not in this agent's allowed_tools. "
                f"configured patterns: {self.patterns or '(none)'}"
            ),
        }


async def _prompt_stream(text: str):
    """Yield a single streaming-mode user message for the SDK's
    AsyncIterable prompt contract. One turn = one user message; we
    embed prior conversation turns inside ``text`` via
    ``format_history_as_prompt``.
    """
    yield {
        "type": "user",
        "message": {"role": "user", "content": text},
        "parent_tool_use_id": None,
        "session_id": "puffoagent-turn",
    }


def _pattern_matches(tool_name: str, tool_input: dict, pattern: str) -> bool:
    """Match one pattern against a tool invocation.

    Supported forms:
      - ``Read``              → exact tool-name match
      - ``Read(**/*.py)``     → tool name + ``file_path``/``path`` glob
      - ``Bash(git *)``       → tool name + ``command`` glob (Bash only)
      - ``*``                 → match anything
    """
    if "(" not in pattern:
        return fnmatch.fnmatch(tool_name, pattern)
    head, rest = pattern.split("(", 1)
    name_pat = head.strip()
    arg_pat = rest.rstrip(")").strip()
    if not fnmatch.fnmatch(tool_name, name_pat):
        return False
    if tool_name == "Bash":
        return fnmatch.fnmatch(tool_input.get("command", ""), arg_pat)
    path = tool_input.get("file_path") or tool_input.get("path") or ""
    return fnmatch.fnmatch(path, arg_pat)
