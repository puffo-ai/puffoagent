"""Claude Code PreToolUse hook — puffoagent permission proxy.

Runs once per tool invocation that claude would normally prompt on
(matcher in the generated settings.json filters reads out). Posts an
"agent X wants to run Y" DM to the operator, polls the thread for a
y/n reply, and returns the decision to claude via the documented
PreToolUse hook protocol:

  exit 2                  → deny (reason on stderr, shown to claude)
  exit 0 + allow JSON     → allow (skip native permission prompt)
  exit 0 + empty stdout   → fall through to normal flow (fail-open)

**Why a hook and not ``--permission-prompt-tool``?** That CLI flag
is documented as non-interactive-mode only (requires ``-p``). Our
cli-local adapter runs claude in a long-lived stream-json session
(interactive mode). ``PreToolUse`` hooks DO run in interactive
mode — same mechanism, different integration point.

Config via env vars set by the spawning adapter:

  PUFFO_URL                Mattermost base URL (required)
  PUFFO_BOT_TOKEN          bot's personal access token (required)
  PUFFO_OPERATOR_USERNAME  who to DM (required; empty → fail-open)
  PUFFO_AGENT_ID           shown in the DM (default "unknown")
  PUFFO_PERMISSION_TIMEOUT poll timeout seconds (default 300)

This module is intentionally stdlib-only (``urllib`` + ``json`` +
``time`` + ``sys`` + ``os``) so it can be invoked from a minimal
interpreter without importing the rest of the puffoagent package.
Sync / one-shot — no asyncio / aiohttp event loop overhead per
tool call.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def _fail_open(reason: str) -> None:
    """Exit 0 with stderr note. Claude proceeds through its normal
    permission flow — for the unsupervised-host cli-local case this
    means claude would try to prompt. Fail-open is the right call
    when the proxy itself can't run (missing config, network down
    before we even posted the request): denying silently every tool
    call on a misconfigured agent creates a worse debugging
    experience than letting claude's native flow surface the issue.
    """
    print(f"[puffo-permission-hook] fail-open: {reason}", file=sys.stderr)
    sys.exit(0)


def _deny(reason: str) -> None:
    """Exit 2 with reason on stderr. Claude prevents the tool call
    and shows the reason back to the model so it can respond to the
    user with context about what was blocked.
    """
    print(reason, file=sys.stderr)
    sys.exit(2)


def _allow(reason: str) -> None:
    """Exit 0 with explicit allow JSON. Without this JSON, claude
    would still run permission checks AFTER the hook — which would
    prompt again. The explicit decision short-circuits that.
    """
    out = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": reason,
        }
    }
    print(json.dumps(out))
    sys.exit(0)


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _http_get(url: str, headers: dict, timeout: float = 10.0):
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_post(url: str, headers: dict, payload, timeout: float = 10.0):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _lookup_operator_id(
    base_url: str, headers: dict, operator_username: str,
) -> str:
    """Return the operator's user id. Used to filter replies in
    the request thread to just the operator's posts.
    """
    user = _http_get(
        f"{base_url}/api/v4/users/username/{operator_username}", headers,
    )
    return user["id"]


def read_current_turn(cwd: str) -> dict | None:
    """Read the per-turn context the daemon wrote before dispatching
    to claude. Returns ``{channel_id, root_id, triggering_post_id}``
    or ``None`` when the file is missing (proactive agent work with
    no user-triggered turn → hook should fail open).
    """
    path = Path(cwd) / ".puffoagent" / "current_turn.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("channel_id"):
        return None
    return data


def summarise_tool_input(data, limit: int = 400) -> str:
    """Human-readable summary of ``tool_input`` for the permission
    DM. Cap per-value at 120 chars and overall at ``limit`` so a
    pasted file or massive command doesn't turn the DM into a wall.
    """
    if isinstance(data, dict):
        parts = []
        for k, v in data.items():
            s = str(v)
            if len(s) > 120:
                s = s[:120] + "…"
            parts.append(f"- **{k}**: `{s}`")
        text = "\n".join(parts)
    elif data is None:
        text = ""
    else:
        text = f"`{str(data)[:limit]}`"
    if len(text) > limit:
        text = text[:limit] + "…"
    return text or "(no input)"


def poll_for_reply(
    base_url: str,
    headers: dict,
    thread_root_id: str,
    owner_id: str,
    request_ts: int,
    timeout_seconds: int,
    sleep_seconds: float = 2.0,
) -> bool | None:
    """Poll the permission-request thread for the operator's reply.

    Returns True on approval (first char y/a), False on denial
    (anything else), None on timeout. The request is a top-level
    DM so its id becomes the thread root — the operator replies
    in-thread, which keeps concurrent tool approvals correlated
    even when claude fires multiple tool calls in parallel.
    """
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            data = _http_get(
                f"{base_url}/api/v4/posts/{thread_root_id}/thread",
                headers,
            )
        except Exception:
            time.sleep(sleep_seconds)
            continue
        posts = data.get("posts") or {}
        order = data.get("order") or []
        for pid in order:
            post = posts.get(pid) or {}
            if post.get("user_id") != owner_id:
                continue
            created_ms = int(post.get("create_at", 0))
            if created_ms // 1000 <= request_ts:
                continue
            msg = (post.get("message") or "").strip().lower()
            if not msg:
                continue
            return msg[0] in ("y", "a")
        time.sleep(sleep_seconds)
    return None


def main() -> None:
    base_url = (os.environ.get("PUFFO_URL") or "").rstrip("/")
    bot_token = os.environ.get("PUFFO_BOT_TOKEN") or ""
    operator = os.environ.get("PUFFO_OPERATOR_USERNAME") or ""
    agent_id = os.environ.get("PUFFO_AGENT_ID") or "unknown"
    try:
        timeout_s = int(os.environ.get("PUFFO_PERMISSION_TIMEOUT") or "300")
    except ValueError:
        timeout_s = 300

    if not (base_url and bot_token):
        _fail_open("PUFFO_URL / PUFFO_BOT_TOKEN not set")
    if not operator:
        _fail_open("PUFFO_OPERATOR_USERNAME empty — no operator to DM")

    try:
        raw = sys.stdin.read() or "{}"
        payload = json.loads(raw)
    except Exception as exc:
        _fail_open(f"could not parse hook payload: {exc}")
    tool_name = payload.get("tool_name", "unknown")
    tool_input = payload.get("tool_input", {})
    # claude passes the subprocess cwd to the hook — which matches
    # the agent's workspace_dir. The daemon drops current_turn.json
    # there so the hook can find the channel + root to reply in.
    cwd = payload.get("cwd", "")

    turn = read_current_turn(cwd)
    if turn is None:
        _fail_open(
            "no current_turn.json — proactive agent work, no user "
            "message to reply to"
        )
    channel_id = turn["channel_id"]
    # ``root_id`` may equal the triggering post id when the user
    # message was itself the root of a (new) thread. Either way,
    # replying with this root_id lands the permission request in
    # the same thread the user is reading.
    root_id = turn.get("root_id") or turn.get("triggering_post_id") or ""
    if not root_id:
        _fail_open("current_turn.json missing root_id")

    headers = _headers(bot_token)
    try:
        operator_id = _lookup_operator_id(base_url, headers, operator)
    except Exception as exc:
        _fail_open(f"cannot look up operator @{operator}: {exc}")

    summary = summarise_tool_input(tool_input)
    request_ts = int(time.time())
    try:
        _http_post(
            f"{base_url}/api/v4/posts",
            headers,
            {
                "channel_id": channel_id,
                "root_id": root_id,
                "message": (
                    f"@{operator} 🔐 **agent `{agent_id}` wants to run "
                    f"`{tool_name}`**\n\n"
                    f"{summary}\n\n"
                    f"Reply `y` to approve or `n` to deny (times out in "
                    f"{timeout_s}s)."
                ),
            },
        )
    except Exception as exc:
        _fail_open(f"could not post permission request: {exc}")

    # Poll the ORIGINAL thread (root_id of the user's triggering
    # message) for the operator's reply. Multiple permission
    # requests within the same turn all share this same thread;
    # the since_ts gate keeps each request's poll scoped to replies
    # posted AFTER it asked.
    decision = poll_for_reply(
        base_url, headers, root_id, operator_id, request_ts, timeout_s,
    )
    if decision is True:
        _allow(f"@{operator} approved via Mattermost")
    if decision is False:
        _deny(f"@{operator} denied via Mattermost")
    _deny(f"permission request timed out after {timeout_s}s (no reply from @{operator})")


if __name__ == "__main__":
    main()
