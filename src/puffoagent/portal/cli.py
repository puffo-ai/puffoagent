"""Top-level CLI for the puffoagent portal.

All commands are file-driven (no IPC). The daemon reconciles on-disk
state every few seconds; CLI subcommands just manipulate files and
read ``runtime.json`` for live stats.

Entry point: the ``puffoagent`` console script installed by pip, or
``python -m puffoagent.portal.cli <subcommand>`` if you prefer invoking
the module directly.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import logging
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

from .daemon import run_daemon
from .state import (
    AgentConfig,
    DaemonConfig,
    MattermostConfig,
    ProviderConfig,
    RuntimeConfig,
    RuntimeState,
    TriggerRules,
    agent_dir,
    agent_yml_path,
    agents_dir,
    archived_dir,
    daemon_yml_path,
    daemon_pid_path,
    discover_agents,
    home_dir,
    is_daemon_alive,
    is_valid_agent_id,
    read_daemon_pid,
)

DEFAULT_PROFILE = """# Agent Profile

## Conversation Format
Every incoming user message is wrapped in a structured markdown block:

    - channel: <channel name>
    - sender: <username> (<email>)
    - message: <actual message text>

The first two fields are context metadata — use them to understand where
the message was posted and who sent it. Only the `message:` field
contains the actual text you are replying to.

IMPORTANT: Your reply must contain ONLY your response text. Do NOT
include the markdown block, field labels like `message:`, bracketed
prefixes like `[#channel]`, or self-identifiers. If you need to address
the sender, use `@username` inline.

## Identity
You are a helpful assistant.

## When to Reply
Use your judgement. Reply when someone directly addresses you or asks a
question that invites a response. Stay silent when the conversation is
between other people and you have nothing useful to add — output
exactly `[SILENT]` to stay silent.
"""


# ─────────────────────────────────────────────────────────────────────────────
# init / start / status
# ─────────────────────────────────────────────────────────────────────────────


def cmd_init(args: argparse.Namespace) -> int:
    home_dir().mkdir(parents=True, exist_ok=True)
    cfg = DaemonConfig.load()

    if daemon_yml_path().exists():
        print(f"daemon.yml already exists at {daemon_yml_path()}")
    else:
        print("creating daemon.yml...")

    env_anthropic = os.environ.get("ANTHROPIC_API_KEY", "")
    env_openai = os.environ.get("OPENAI_API_KEY", "")

    def prompt(label: str, default: str = "") -> str:
        hint = f" [{default}]" if default else ""
        try:
            val = input(f"{label}{hint}: ").strip()
        except EOFError:
            val = ""
        return val or default

    cfg.default_provider = prompt("Default AI provider (anthropic|openai)", cfg.default_provider or "anthropic")

    anth_key = cfg.anthropic.api_key or env_anthropic
    anth_key = prompt("Anthropic API key (blank to skip)", anth_key)
    if anth_key:
        cfg.anthropic = ProviderConfig(api_key=anth_key, model=cfg.anthropic.model or "claude-sonnet-4-6")

    oai_key = cfg.openai.api_key or env_openai
    oai_key = prompt("OpenAI API key (blank to skip)", oai_key)
    if oai_key:
        cfg.openai = ProviderConfig(api_key=oai_key, model=cfg.openai.model or "gpt-4o")

    cfg.save()
    print(f"wrote {daemon_yml_path()}")
    print(f"agents dir: {agents_dir()}")
    print()
    print("agent runtime choices (per agent, set at create time):")
    print("  chat-only    conversational LLM, no tools (default, uses the keys above)")
    print("  sdk          in-process Claude agent loop w/ tools  [pip install puffoagent[sdk]]")
    print("  cli-local    claude CLI on the host, --dangerously-skip-permissions [run `claude login` first]")
    print("  cli-docker   claude CLI inside a per-agent container  [Docker + `claude login` on host]")
    print()
    print("next: puffoagent agent create --id <id> --runtime <kind> \\")
    print("        --url <mm url> --token <bot token> --channels general,random")
    return 0


def cmd_login(args: argparse.Namespace) -> int:
    """Store a Puffo server URL + user token for server-synced mode."""
    home_dir().mkdir(parents=True, exist_ok=True)
    cfg = DaemonConfig.load()
    url = args.url or ""
    token = args.token or ""
    if not url:
        url = input("Puffo server URL [http://localhost:8065]: ").strip() or "http://localhost:8065"
    if not token:
        token = input("User personal access token: ").strip()
    if not token:
        print("error: user token is required", file=sys.stderr)
        return 2
    # Sanity check with a GET /users/me.
    import urllib.request
    import urllib.error
    req = urllib.request.Request(
        url.rstrip("/") + "/api/v4/users/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            import json as _json
            me = _json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        print(f"error: server rejected token ({exc.code} {exc.reason})", file=sys.stderr)
        return 2
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"error: cannot reach {url}: {exc}", file=sys.stderr)
        return 2
    cfg.server.url = url.rstrip("/")
    cfg.server.user_token = token
    cfg.save()
    print(f"logged in as @{me.get('username', '?')} ({me.get('email', '?')})")
    print(f"server sync will run on next `puffoagent start`.")
    return 0


def cmd_logout(args: argparse.Namespace) -> int:
    cfg = DaemonConfig.load()
    if not cfg.has_server_sync():
        print("(not logged in)")
        return 0
    cfg.server.url = ""
    cfg.server.user_token = ""
    cfg.save()
    print("logged out; server sync disabled")
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    return asyncio.run(run_daemon())


def cmd_status(args: argparse.Namespace) -> int:
    pid = read_daemon_pid()
    alive = is_daemon_alive()
    if alive and pid is not None:
        print(f"daemon: running (pid={pid})")
    elif pid is not None:
        print(f"daemon: not running (stale pid file at {daemon_pid_path()}; pid={pid})")
    else:
        print("daemon: not running")
    cfg = DaemonConfig.load()
    if cfg.has_server_sync():
        print(f"server sync: {cfg.server.url} (interval={int(cfg.server.sync_interval_seconds)}s)")
    else:
        print("server sync: disabled (run `puffoagent login`)")
    agents = discover_agents()
    print(f"home: {home_dir()}")
    print(f"agents registered: {len(agents)}")
    for aid in agents:
        try:
            ac = AgentConfig.load(aid)
            rs = RuntimeState.load(aid)
            status = rs.status if rs else "unknown"
            print(f"  - {aid}  state={ac.state}  runtime={status}")
        except Exception as exc:
            print(f"  - {aid}  (error: {exc})")
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# agent subcommands
# ─────────────────────────────────────────────────────────────────────────────


def cmd_agent_create(args: argparse.Namespace) -> int:
    agent_id = args.id
    if not is_valid_agent_id(agent_id):
        print(f"error: invalid agent id {agent_id!r} (alphanumerics, _ and -)", file=sys.stderr)
        return 2
    target = agent_dir(agent_id)
    if target.exists():
        print(f"error: agent {agent_id!r} already exists at {target}", file=sys.stderr)
        return 2
    target.mkdir(parents=True)

    channels = [c.strip() for c in (args.channels or "").split(",") if c.strip()]

    cfg = AgentConfig(
        id=agent_id,
        state="running",
        display_name=args.display_name or agent_id,
        mattermost=MattermostConfig(
            url=args.url,
            bot_token=args.token,
            team_name=args.team or "",
        ),
        runtime=RuntimeConfig(
            kind=args.runtime or "chat-only",
            provider=args.provider or "",
            api_key=args.api_key or "",
            model=args.model or "",
        ),
        profile="profile.md",
        memory_dir="memory",
        workspace_dir="workspace",
        triggers=TriggerRules(
            on_mention=not args.no_mention,
            on_dm=not args.no_dm,
        ),
        created_at=int(time.time()),
    )
    cfg.save()

    (target / "memory").mkdir(exist_ok=True)

    profile_path = target / "profile.md"
    if args.profile and Path(args.profile).exists():
        shutil.copy2(args.profile, profile_path)
    else:
        profile_path.write_text(DEFAULT_PROFILE, encoding="utf-8")

    print(f"created agent {agent_id!r} at {target}")
    if channels:
        print(f"note: channels list ({channels}) is informational — the bot's")
        print("      messages come from whatever channels the bot account has")
        print("      been added to on the Mattermost server.")
    if not is_daemon_alive():
        print("daemon is not running — run `puffoagent start` to activate.")
    else:
        print("daemon will pick it up on the next reconcile tick (a few seconds).")
    return 0


def cmd_agent_list(args: argparse.Namespace) -> int:
    agents = discover_agents()
    if not agents:
        print("(no agents registered)")
        return 0
    daemon_alive = is_daemon_alive()
    fmt = "{id:<20}  {state:<8}  {runtime:<10}  {msgs:>6}  {uptime}"
    print(fmt.format(id="ID", state="STATE", runtime="RUNTIME", msgs="MSGS", uptime="UPTIME"))
    print("-" * 72)
    for aid in agents:
        try:
            ac = AgentConfig.load(aid)
        except Exception as exc:
            print(f"{aid:<20}  (error: {exc})")
            continue
        rs = RuntimeState.load(aid)
        if rs is None:
            runtime = "no data"
            msgs = 0
            uptime = "—"
        else:
            staleness = int(time.time()) - rs.updated_at
            if daemon_alive and staleness < 30:
                runtime = rs.status
            elif rs.status == "stopped":
                runtime = "stopped"
            else:
                runtime = "stale"
            msgs = rs.msg_count
            if rs.started_at:
                uptime = _format_duration(int(time.time()) - rs.started_at)
            else:
                uptime = "—"
        print(fmt.format(id=aid, state=ac.state, runtime=runtime, msgs=msgs, uptime=uptime))
    return 0


def cmd_agent_show(args: argparse.Namespace) -> int:
    agent_id = args.id
    if not agent_yml_path(agent_id).exists():
        print(f"error: agent {agent_id!r} not found", file=sys.stderr)
        return 2
    ac = AgentConfig.load(agent_id)
    rs = RuntimeState.load(agent_id)
    print(f"id:              {ac.id}")
    print(f"display_name:    {ac.display_name}")
    print(f"state:           {ac.state}")
    print(f"directory:       {agent_dir(agent_id)}")
    print(f"profile:         {ac.resolve_profile_path()}")
    print(f"memory_dir:      {ac.resolve_memory_dir()}")
    print(f"mattermost url:  {ac.mattermost.url}")
    print(f"mattermost team: {ac.mattermost.team_name or '(not set)'}")
    print(f"workspace_dir:   {ac.resolve_workspace_dir()}")
    print(f"claude_dir:      {ac.resolve_claude_dir()}  (derived)")
    print("runtime:")
    print(f"  kind:          {ac.runtime.kind}")
    print(f"  provider:      {ac.runtime.provider or '(default)'}")
    print(f"  model:         {ac.runtime.model or '(default)'}")
    print(f"  api_key:       {'(set)' if ac.runtime.api_key else '(inherit)'}")
    print(f"triggers:        on_mention={ac.triggers.on_mention} on_dm={ac.triggers.on_dm}")
    if rs is not None:
        print("status:")
        print(f"  status:        {rs.status}")
        print(f"  msg_count:     {rs.msg_count}")
        print(f"  last_event_at: {_format_ts(rs.last_event_at)}")
        print(f"  updated_at:    {_format_ts(rs.updated_at)}")
        if rs.error:
            print(f"  error:         {rs.error}")
    return 0


def cmd_agent_pause(args: argparse.Namespace) -> int:
    return _set_agent_state(args.id, "paused")


def cmd_agent_resume(args: argparse.Namespace) -> int:
    return _set_agent_state(args.id, "running")


def _set_agent_state(agent_id: str, new_state: str) -> int:
    if not agent_yml_path(agent_id).exists():
        print(f"error: agent {agent_id!r} not found", file=sys.stderr)
        return 2
    cfg = AgentConfig.load(agent_id)
    if cfg.state == new_state:
        print(f"agent {agent_id!r} already {new_state}")
        return 0
    cfg.state = new_state
    cfg.save()
    print(f"agent {agent_id!r} state set to {new_state}")
    if is_daemon_alive():
        print("daemon will apply the change on the next reconcile tick.")
    return 0


def cmd_agent_archive(args: argparse.Namespace) -> int:
    agent_id = args.id
    src = agent_dir(agent_id)
    if not src.exists():
        print(f"error: agent {agent_id!r} not found", file=sys.stderr)
        return 2
    # Ask the daemon to stop it first by flipping state to paused, so the
    # worker exits cleanly before we move the directory.
    cfg = AgentConfig.load(agent_id)
    if cfg.state != "paused":
        cfg.state = "paused"
        cfg.save()
        print(f"flipped {agent_id!r} to paused; waiting for daemon to release it...")
        for _ in range(10):
            rs = RuntimeState.load(agent_id)
            if rs is None or rs.status in ("stopped", "paused"):
                break
            time.sleep(1)

    archived_dir().mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = archived_dir() / f"{agent_id}-{stamp}"
    shutil.move(str(src), str(dest))
    print(f"archived {agent_id!r} → {dest}")
    return 0


def cmd_agent_edit(args: argparse.Namespace) -> int:
    agent_id = args.id
    if not agent_yml_path(agent_id).exists():
        print(f"error: agent {agent_id!r} not found", file=sys.stderr)
        return 2
    cfg = AgentConfig.load(agent_id)
    profile = cfg.resolve_profile_path()
    editor = os.environ.get("EDITOR") or ("notepad" if os.name == "nt" else "vi")
    try:
        subprocess.call([editor, str(profile)])
    except FileNotFoundError:
        print(f"error: editor {editor!r} not found. Set $EDITOR and retry.", file=sys.stderr)
        return 2
    return 0


def cmd_agent_export(args: argparse.Namespace) -> int:
    agent_id = args.id
    src = agent_dir(agent_id)
    if not src.exists():
        print(f"error: agent {agent_id!r} not found", file=sys.stderr)
        return 2
    dest = Path(args.dest)
    if dest.suffix.lower() != ".zip":
        dest = dest.with_suffix(".zip")
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in src.rglob("*"):
            if path.is_file():
                # Skip any tmp files mid-write.
                if path.suffix == ".tmp":
                    continue
                arcname = Path(agent_id) / path.relative_to(src)
                zf.write(path, arcname=str(arcname))
    print(f"exported {agent_id!r} → {dest}")
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _format_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60}s"
    hours, rem = divmod(seconds, 3600)
    return f"{hours}h{rem // 60}m"


def _format_ts(ts: int) -> str:
    if not ts:
        return "never"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


# ─────────────────────────────────────────────────────────────────────────────
# argparse glue
# ─────────────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="puffoagent",
        description="Multi-agent portal for Puffo.ai",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="Set up ~/.puffoagent/daemon.yml interactively").set_defaults(func=cmd_init)
    sub.add_parser("start", help="Run the daemon in the foreground").set_defaults(func=cmd_start)
    sub.add_parser("status", help="Show daemon + agent status").set_defaults(func=cmd_status)

    login = sub.add_parser("login", help="Store a Puffo server URL + user token for server-synced mode")
    login.add_argument("--url", help="Puffo server URL")
    login.add_argument("--token", help="User personal access token")
    login.set_defaults(func=cmd_login)

    sub.add_parser("logout", help="Clear server URL + user token (disable sync)").set_defaults(func=cmd_logout)

    agent = sub.add_parser("agent", help="Manage individual agents")
    agent_sub = agent.add_subparsers(dest="agent_cmd", required=True)

    create = agent_sub.add_parser("create", help="Register a new agent")
    create.add_argument("--id", required=True)
    create.add_argument("--url", required=True, help="Mattermost URL (e.g. http://localhost:8065)")
    create.add_argument("--token", required=True, help="Bot account personal access token")
    create.add_argument("--team", help="Mattermost team name (informational)")
    create.add_argument("--channels", help="Comma-separated channel names (informational)")
    create.add_argument("--display-name", help="Friendly name for the agent")
    create.add_argument("--profile", help="Path to a profile.md to copy (default: built-in template)")
    create.add_argument(
        "--runtime",
        choices=["chat-only", "sdk", "cli-local", "cli-docker"],
        default="chat-only",
        help="Runtime adapter kind (default: chat-only)",
    )
    create.add_argument("--provider", help="Chat-only: provider override (anthropic|openai)")
    create.add_argument("--api-key", help="Provider/runtime API key override")
    create.add_argument("--model", help="Model override")
    create.add_argument("--no-mention", action="store_true", help="Don't reply on @mention")
    create.add_argument("--no-dm", action="store_true", help="Don't reply on DM")
    create.set_defaults(func=cmd_agent_create)

    lst = agent_sub.add_parser("list", help="List registered agents")
    lst.set_defaults(func=cmd_agent_list)

    show = agent_sub.add_parser("show", help="Show details for one agent")
    show.add_argument("id")
    show.set_defaults(func=cmd_agent_show)

    pause = agent_sub.add_parser("pause", help="Pause a running agent (daemon will stop its worker)")
    pause.add_argument("id")
    pause.set_defaults(func=cmd_agent_pause)

    resume = agent_sub.add_parser("resume", help="Resume a paused agent")
    resume.add_argument("id")
    resume.set_defaults(func=cmd_agent_resume)

    archive = agent_sub.add_parser("archive", help="Stop and archive an agent to ~/.puffoagent/archived/")
    archive.add_argument("id")
    archive.set_defaults(func=cmd_agent_archive)

    edit = agent_sub.add_parser("edit", help="Open the agent's profile.md in $EDITOR")
    edit.add_argument("id")
    edit.set_defaults(func=cmd_agent_edit)

    export = agent_sub.add_parser("export", help="Export agent profile + memory + config as a zip")
    export.add_argument("id")
    export.add_argument("dest", help="Destination .zip file")
    export.set_defaults(func=cmd_agent_export)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
