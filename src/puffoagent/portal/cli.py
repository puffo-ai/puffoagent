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
    agent_home_dir,
    agent_yml_path,
    agents_dir,
    archived_dir,
    daemon_yml_path,
    daemon_pid_path,
    discover_agents,
    home_dir,
    is_daemon_alive,
    is_valid_agent_id,
    link_host_credentials,
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
# Version + update helpers
# ─────────────────────────────────────────────────────────────────────────────

# Where we ask "what's the latest released version?". GitHub Releases
# is the source of truth for end users (PyPI lags behind the repo
# tags during a release window). Switched here so we don't have to
# fetch + parse PyPI JSON, which is rate-limited per IP.
GITHUB_RELEASES_LATEST_URL = (
    "https://api.github.com/repos/puffo-ai/puffoagent/releases/latest"
)


def get_local_version() -> str:
    """Return the installed puffoagent version, or "unknown" if the
    package metadata can't be located (e.g. running from a checkout
    without `pip install`).
    """
    try:
        from importlib.metadata import PackageNotFoundError, version

        return version("puffoagent")
    except (ImportError, Exception):
        return "unknown"


def is_source_install() -> bool:
    """True when puffoagent was installed from a local path or VCS
    rather than from PyPI. Detected via PEP 610's
    ``direct_url.json``, which pip drops next to the dist-info for
    any non-PyPI install (`pip install /path/to/dir`,
    `pip install -e .`, `pip install git+https://...`).

    The startup version-check skips outdated warnings for source
    installs so a developer working off a feature branch isn't
    spammed with "you're behind main" when they're ahead of main.
    """
    try:
        from importlib.metadata import files

        for f in files("puffoagent") or []:
            if f.name == "direct_url.json":
                return True
    except Exception:
        pass
    return False


def fetch_latest_release_tag(timeout: float = 5.0) -> str | None:
    """GET the latest release tag from GitHub. Returns None on any
    failure (offline, rate-limited, GitHub outage, malformed body) so
    the caller can fail-soft. Stripped of a leading ``v`` so tags
    like ``v0.4.0`` compare cleanly against the installed
    ``0.4.0``.
    """
    import json as _json
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        GITHUB_RELEASES_LATEST_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "puffoagent-cli",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
        tag = (data.get("tag_name") or "").strip()
        return tag.lstrip("v") or None
    except (urllib.error.URLError, urllib.error.HTTPError,
            TimeoutError, ValueError, OSError):
        return None


def is_outdated(local: str, remote: str) -> bool:
    """``remote > local`` for simple dotted versions like ``0.3.1``.
    Tolerates trailing pre-release suffixes (``0.4.0rc1`` → 0.4.0)
    by parsing only the leading digits of each segment. Falls back
    to False on anything we can't parse — better to under-warn
    than to warn on noise.
    """
    def parse(v: str) -> tuple[int, ...]:
        out: list[int] = []
        for part in v.split("."):
            digits = ""
            for ch in part:
                if ch.isdigit():
                    digits += ch
                else:
                    break
            out.append(int(digits) if digits else 0)
        return tuple(out)

    if local in ("", "unknown") or not remote:
        return False
    try:
        return parse(remote) > parse(local)
    except Exception:
        return False


def upgrade_command_for_install_mode() -> str:
    """Return the shell command a user should run to upgrade their
    install. Source installs need the local path; PyPI installs use
    the package name. We can't always tell exactly *which* source
    path they used, so for source installs we recommend re-running
    pip with the same path they originally cloned to (we point
    them at the standard public repo URL as a hint).
    """
    if is_source_install():
        return (
            "pip install --upgrade --user "
            "git+https://github.com/puffo-ai/puffoagent.git"
        )
    return "pip install --upgrade puffoagent"


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
    """Connect this machine to a Puffo.ai server.

    Two modes:
      - Manual:  ``--url <X> --token <Y>`` — caller already has a
                 personal access token (legacy / scripted install).
      - Pairing: just ``--url <X>`` (or no args, in which case we
                 prompt for the URL) — start an OAuth-style device
                 flow. Server returns a short user_code; the user
                 enters it at <url>/cli/connect in any browser
                 they're already logged into. CLI polls until the
                 user authorizes, then receives + saves a token
                 issued for THIS machine.
    """
    home_dir().mkdir(parents=True, exist_ok=True)
    cfg = DaemonConfig.load()
    url = (args.url or "").rstrip("/")
    token = args.token or ""

    # Token explicitly provided → manual path (no device pairing).
    if token:
        if not url:
            url = input("Puffo server URL [http://localhost:8065]: ").strip().rstrip("/") or "http://localhost:8065"
        return _login_with_token(cfg, url, token)

    if not url:
        url = input("Puffo server URL [http://localhost:8065]: ").strip().rstrip("/") or "http://localhost:8065"
    return _login_via_device_pairing(cfg, url)


def _login_with_token(cfg: DaemonConfig, url: str, token: str) -> int:
    """Legacy manual-token login. Verifies the token by hitting
    /users/me and saves on success.
    """
    import json as _json
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        url + "/api/v4/users/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            me = _json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        print(f"error: server rejected token ({exc.code} {exc.reason})", file=sys.stderr)
        return 2
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"error: cannot reach {url}: {exc}", file=sys.stderr)
        return 2
    cfg.server.url = url
    cfg.server.user_token = token
    cfg.server.device_id = ""  # legacy login isn't device-bound
    cfg.server.operator_username = me.get("username", "") or ""
    cfg.save()
    print(f"logged in as @{me.get('username', '?')} ({me.get('email', '?')})")
    print(f"server sync will run on next `puffoagent start`.")
    return 0


def _login_via_device_pairing(cfg: DaemonConfig, url: str) -> int:
    """Run the OAuth-style device-code flow. Steps:

      1. POST /cli/devices/pair/start with this machine's hostname.
         Server returns device_code (kept secret) + user_code (shown
         to the user) + verification_url + ttl + poll interval.
      2. Print the user_code + URL. User opens URL in any browser
         they're already authenticated to and clicks Authorize.
      3. Poll /cli/devices/pair/poll every Interval seconds until
         the response status is 'authorized' (we got the token),
         'denied', or 'expired'.
      4. On success, save the issued access_token + device_id to
         daemon.yml.
    """
    import json as _json
    import socket
    import time
    import urllib.error
    import urllib.request

    device_name = socket.gethostname() or "unknown-host"

    # Step 1: start.
    # Verbose tracing is opt-in via env var so the diagnostic output
    # doesn't clutter normal runs. Set PUFFOAGENT_LOGIN_DEBUG=1 to
    # see the full request/response roundtrip.
    debug = os.environ.get("PUFFOAGENT_LOGIN_DEBUG") == "1"

    def _trace(msg: str) -> None:
        if debug:
            print(f"[debug] {msg}", file=sys.stderr, flush=True)

    start_url = url + "/api/v4/cli/devices/pair/start"
    start_body = _json.dumps({"device_name": device_name}).encode("utf-8")
    _trace(f"POST {start_url} body={start_body.decode('utf-8')}")
    start_req = urllib.request.Request(
        start_url,
        data=start_body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(start_req, timeout=10) as resp:
            start_raw = resp.read().decode("utf-8")
            _trace(f"pair/start response: {start_raw}")
            start = _json.loads(start_raw)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:200]
        print(f"error: pair start failed ({exc.code}): {body}", file=sys.stderr)
        return 2
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"error: cannot reach {url}: {exc}", file=sys.stderr)
        return 2

    device_code = start.get("device_code", "")
    user_code = start.get("user_code", "")
    verification_url_base = start.get("verification_url", url + "/cli/connect")
    expires_in = int(start.get("expires_in", 120))
    interval = max(1, int(start.get("interval", 2)))
    _trace(f"device_code len={len(device_code)} head={device_code[:12]}... tail=...{device_code[-12:]}")
    _trace(f"user_code={user_code!r}")
    _trace(f"verification_url_base={verification_url_base!r}")

    if not device_code or not user_code:
        print("error: server returned an incomplete pairing response", file=sys.stderr)
        return 2

    # Pre-fill the code in the URL so the operator just clicks
    # Authorize instead of copy-pasting. ``?code=`` is read by the
    # /cli/connect webapp page.
    sep = "&" if "?" in verification_url_base else "?"
    verification_url = f"{verification_url_base}{sep}code={user_code}"

    # Step 2: ask the human. Try to open the page automatically —
    # most desktops have a default browser and this removes the
    # "oh I have to click a URL" step. Fall back silently: print
    # the URL either way so SSH / headless sessions still work.
    import webbrowser
    browser_opened = False
    try:
        browser_opened = webbrowser.open(verification_url, new=2)
    except Exception:
        browser_opened = False

    print()
    if browser_opened:
        print("Opened in your browser (if it didn't appear, the URL is below).")
    else:
        print("Could not open a browser automatically — open this URL:")
    print(f"  {verification_url}")
    print()
    print(f"Code:  {user_code}")
    print()
    print(f"Waiting for authorization (expires in {expires_in}s)...", flush=True)

    # Step 3: poll.
    poll_url = url + "/api/v4/cli/devices/pair/poll"
    deadline = time.time() + expires_in
    poll_body = _json.dumps({"device_code": device_code}).encode("utf-8")
    _trace(f"poll URL: {poll_url}")
    _trace(f"poll body: {poll_body.decode('utf-8')}")
    while time.time() < deadline:
        time.sleep(interval)
        poll_req = urllib.request.Request(
            poll_url,
            data=poll_body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(poll_req, timeout=10) as resp:
                poll_raw = resp.read().decode("utf-8")
                _trace(f"poll response: {poll_raw}")
                poll = _json.loads(poll_raw)
        except urllib.error.HTTPError as exc:
            # 404 means the pairing got cleaned up — treat as expired.
            err_body = exc.read().decode("utf-8", errors="replace")[:400]
            print(f"error: poll failed ({exc.code}): {err_body}", file=sys.stderr)
            return 2
        except (urllib.error.URLError, TimeoutError) as exc:
            # Transient — keep polling. The deadline check bounds total time.
            print(f"warning: poll error: {exc}; retrying...", file=sys.stderr)
            continue

        status = poll.get("status", "")
        if status == "authorized":
            access_token = poll.get("access_token", "")
            device_id = poll.get("device_id", "")
            server_url = poll.get("server_url", url).rstrip("/")
            if not access_token:
                print("error: server returned authorized but no access_token", file=sys.stderr)
                return 2
            # Resolve the operator's username with the fresh token so
            # the cli-local permission proxy has a DM target for tool-
            # approval prompts. Soft-fail — if this /users/me call hits
            # a network blip, we still save the login and let the user
            # re-login later rather than failing the whole pairing.
            operator_username = ""
            try:
                me_req = urllib.request.Request(
                    server_url + "/api/v4/users/me",
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                with urllib.request.urlopen(me_req, timeout=10) as me_resp:
                    me = _json.loads(me_resp.read().decode("utf-8"))
                operator_username = me.get("username", "") or ""
            except (urllib.error.HTTPError, urllib.error.URLError,
                    TimeoutError, ValueError) as exc:
                print(
                    f"warning: paired OK but could not resolve operator username: {exc}",
                    file=sys.stderr,
                )
            cfg.server.url = server_url
            cfg.server.user_token = access_token
            cfg.server.device_id = device_id
            cfg.server.operator_username = operator_username
            cfg.save()
            print()
            print(f"paired this machine ({device_name}) — device_id={device_id}")
            if operator_username:
                print(f"operator:              @{operator_username}")
            print(f"server sync will run on next `puffoagent start`.")
            return 0
        if status == "denied":
            print("error: pairing denied by user", file=sys.stderr)
            return 2
        if status == "expired":
            print("error: pairing expired before authorization. re-run `puffoagent login`.", file=sys.stderr)
            return 2
        # Otherwise status == "pending" — keep polling.

    print("error: pairing timed out before authorization. re-run `puffoagent login`.", file=sys.stderr)
    return 2


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


def cmd_version(args: argparse.Namespace) -> int:
    """Print the installed puffoagent version and where it came
    from. Cheap, offline, idempotent — useful first command when
    debugging "why doesn't feature X work" reports.
    """
    local = get_local_version()
    src = "source install" if is_source_install() else "release install"
    print(f"puffoagent {local}  ({src})")
    return 0


def cmd_check_update(args: argparse.Namespace) -> int:
    """Compare the installed version against the latest GitHub
    release and print the upgrade command (if outdated) or a
    "you're current" line (if not). Never executes the upgrade
    itself — on Windows the running daemon holds a lock on
    ``puffoagent.exe``, and source vs PyPI installs need different
    pip invocations, so the user is the right party to run it.
    """
    local = get_local_version()
    src = "source install" if is_source_install() else "release install"
    print(f"installed: puffoagent {local}  ({src})")
    remote = fetch_latest_release_tag()
    if remote is None:
        print("latest:    (could not reach github.com — check your network)")
        return 0
    print(f"latest:    {remote}")
    if is_outdated(local, remote):
        print()
        print("an update is available. to upgrade:")
        print(f"  {upgrade_command_for_install_mode()}")
        if is_source_install():
            print("  (or re-run pip install against your local clone)")
        print()
        print("note: if the daemon is currently running, stop it first —")
        print("on windows the puffoagent.exe file is locked while in use.")
        return 0
    print()
    print("you're up to date.")
    return 0


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
            health = rs.health if rs else "unknown"
            # Only surface health when it's not "ok" — keeps the
            # happy-path listing tight while making auth_failed
            # jump out in a quick scan.
            health_suffix = f"  health={health}" if health not in ("ok", "unknown") else ""
            print(f"  - {aid}  state={ac.state}  runtime={status}{health_suffix}")
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
    daemon_cfg = DaemonConfig.load()
    sync_url = (daemon_cfg.server.url or "").rstrip("/").lower()
    fmt = "{id:<20}  {state:<8}  {runtime:<18}  {msgs:>6}  {uptime}"
    print(fmt.format(id="ID", state="STATE", runtime="RUNTIME", msgs="MSGS", uptime="UPTIME"))
    print("-" * 80)
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
        # Mark agents whose mattermost.url disagrees with the daemon's
        # current sync URL so it's obvious which ones won't get
        # server-side updates this session.
        agent_url = (ac.mattermost.url or "").rstrip("/").lower()
        if sync_url and agent_url and agent_url != sync_url:
            runtime = f"{runtime} (other-server)"
        # Surface auth_failed alongside the lifecycle status so one
        # glance at ``puffoagent agent list`` tells the operator
        # which agents need re-auth. "ok" and "unknown" stay silent.
        if rs is not None and rs.health == "auth_failed":
            runtime = f"{runtime} [auth_failed]"
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
        print(f"  health:        {rs.health}")
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


def _summarise_credentials(path: Path) -> str:
    """Human-readable one-liner about a ``.credentials.json`` file:
    mtime, expiresAt, presence of access + refresh tokens, scopes.
    Used by the ``refresh-ping`` diagnostic.
    """
    import json as _json
    if not path.exists():
        return "not present"
    try:
        st = path.stat()
    except OSError as exc:
        return f"stat failed: {exc}"
    try:
        data = _json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return f"size={st.st_size}B mtime={_format_ts(int(st.st_mtime))} parse-error: {exc}"
    oauth = data.get("claudeAiOauth") or {}
    expires_ms = oauth.get("expiresAt")
    if isinstance(expires_ms, (int, float)):
        expires_in = int(expires_ms / 1000 - time.time())
        expires_at = _format_ts(int(expires_ms / 1000))
        expiry_info = f"expiresAt={expires_at} ({expires_in:+d}s from now)"
    else:
        expiry_info = "expiresAt=(missing)"
    has_access = bool(oauth.get("accessToken"))
    has_refresh = bool(oauth.get("refreshToken"))
    scopes = oauth.get("scopes") or []
    return (
        f"mtime={_format_ts(int(st.st_mtime))} {expiry_info} "
        f"accessToken={'yes' if has_access else 'no'} "
        f"refreshToken={'yes' if has_refresh else 'no'} "
        f"scopes={scopes!r}"
    )


def cmd_agent_refresh_ping(args: argparse.Namespace) -> int:
    """Run the OAuth refresh one-shot against a cli-local agent and
    dump everything we can see — credentials before + after, full
    subprocess stdout/stderr — so an operator can diagnose refresh
    failures reproducibly.

    Side effect: whatever the one-shot itself does. Normally this is
    a token rotation that rewrites the agent's ``.credentials.json``;
    if OAuth is unhealthy (expired refresh token, stale refresh chain),
    it's a single failed round-trip that we surface in full.
    """
    agent_id = args.id
    if not agent_yml_path(agent_id).exists():
        print(f"error: agent {agent_id!r} not found", file=sys.stderr)
        return 2
    cfg = AgentConfig.load(agent_id)
    if cfg.runtime.kind != "cli-local":
        print(
            f"error: refresh-ping only supports cli-local right now "
            f"(agent {agent_id!r} is {cfg.runtime.kind!r}). cli-docker "
            "has its own path via `docker exec` that isn't wired here yet.",
            file=sys.stderr,
        )
        return 2

    home_override = agent_home_dir(agent_id)
    agent_creds = home_override / ".claude" / ".credentials.json"
    host_creds = Path.home() / ".claude" / ".credentials.json"
    workspace = Path(cfg.resolve_workspace_dir())

    print(f"agent: {agent_id}  runtime: {cfg.runtime.kind}  model: {cfg.runtime.model or '(default)'}")
    print(f"agent HOME override: {home_override}")
    print(f"workspace:           {workspace}")
    print()
    print("Before link:")
    print(f"  agent {agent_creds}")
    print(f"        {_summarise_credentials(agent_creds)}")
    print(f"  host  {host_creds}")
    print(f"        {_summarise_credentials(host_creds)}")
    print()

    # Mirror what LocalCLIAdapter._verify() does in production so the
    # diagnostic reflects the same starting conditions as a real
    # refresh ping.
    link_mode = link_host_credentials(Path.home(), home_override)
    print(f"link_host_credentials -> {link_mode}")
    print("After link:")
    print(f"  agent {agent_creds}")
    print(f"        {_summarise_credentials(agent_creds)}")
    print()

    if shutil.which("claude") is None:
        print("error: claude binary not on PATH", file=sys.stderr)
        return 2

    cmd = [
        "claude", "--dangerously-skip-permissions",
        "--print", "--max-turns", "1",
        "--output-format", "stream-json", "--verbose",
    ]
    if cfg.runtime.model:
        cmd.extend(["--model", cfg.runtime.model])
    cmd.append("ok")

    env = {
        **os.environ,
        "HOME": str(home_override),
        "USERPROFILE": str(home_override),
    }

    print(f"running: {' '.join(cmd)}")
    print(f"  cwd={workspace}")
    print(f"  HOME={home_override}  USERPROFILE={home_override}")
    print()

    started = time.time()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(workspace),
            env=env,
            capture_output=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired as exc:
        elapsed = time.time() - started
        print(f"timed out after {elapsed:.1f}s", file=sys.stderr)
        if exc.stdout:
            print("--- stdout ---")
            print(exc.stdout.decode("utf-8", errors="replace"))
        if exc.stderr:
            print("--- stderr ---")
            print(exc.stderr.decode("utf-8", errors="replace"))
        return 3
    elapsed = time.time() - started

    print(f"rc={proc.returncode}  elapsed={elapsed:.1f}s")
    print()
    print("--- stdout ---")
    stdout = proc.stdout.decode("utf-8", errors="replace")
    print(stdout or "(empty)")
    print()
    print("--- stderr ---")
    stderr = proc.stderr.decode("utf-8", errors="replace")
    print(stderr or "(empty)")
    print()
    print("After:")
    print(f"  agent {agent_creds}")
    print(f"        {_summarise_credentials(agent_creds)}")
    print(f"  host  {host_creds}")
    print(f"        {_summarise_credentials(host_creds)}")
    return 0


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


def cmd_agent_rename(args: argparse.Namespace) -> int:
    """Change an agent's display name.

    Mattermost bot users render their ``first_name`` as the chat
    display name (``displayUsername()`` in the webapp prefers
    ``getFullName(user)`` for bots). We patch the bot user's
    ``first_name`` via ``PUT /api/v4/users/<bot-user-id>/patch``
    using the bot's own token — no admin user-token required, no
    puffo-specific server endpoint needed. Also mirror the new name
    into ``agent.yml`` so ``agent show`` reflects it immediately.

    Note: the server's AIAgents table has its own ``display_name``
    column that this command does NOT update — that would require a
    puffo-ai server-side endpoint. Until that lands, the next sync
    tick will overwrite local ``agent.yml`` with the (still old)
    aiagent display_name. The chat-visible name stays correct
    regardless because that reads from the bot user's first_name.
    """
    agent_id = args.id
    new_name = (args.display_name or "").strip()
    if not new_name:
        print("error: display_name cannot be empty", file=sys.stderr)
        return 2
    if not agent_yml_path(agent_id).exists():
        print(f"error: agent {agent_id!r} not found", file=sys.stderr)
        return 2
    cfg = AgentConfig.load(agent_id)
    # Mattermost refuses to let a bot patch its own profile (403
    # api.context.permissions.app_error), so we use the operator's
    # admin user token from daemon.yml instead. That token has
    # owner/admin rights over bots they own.
    daemon = DaemonConfig.load()
    if not daemon.has_server_sync():
        print(
            "error: not logged in to a Puffo.ai server. run "
            "`puffoagent login --url ... --token ...` first so we "
            "have an admin token to rename the bot with.",
            file=sys.stderr,
        )
        return 2
    bot_user_id = _resolve_bot_user_id(cfg.mattermost)
    if not bot_user_id:
        print("error: could not resolve bot user id — is the bot token valid?", file=sys.stderr)
        return 2
    ok, err = _patch_user_first_name(
        cfg.mattermost.url, daemon.server.user_token,
        bot_user_id, new_name,
    )
    if not ok:
        print(f"error: server rejected rename ({err})", file=sys.stderr)
        return 2
    cfg.display_name = new_name
    cfg.save()
    print(f"agent {agent_id!r} display_name set to {new_name!r}")
    return 0


def cmd_agent_avatar(args: argparse.Namespace) -> int:
    """Upload a profile picture for the agent's underlying bot user.

    Uses the operator's admin user token from ``daemon.yml`` — same
    reason as ``agent rename``: Mattermost refuses to let a bot
    modify its own profile (403 api.context.permissions), so the
    owner performs the change.
    """
    agent_id = args.id
    image_path = Path(args.image).expanduser()
    if not image_path.is_file():
        print(f"error: {image_path} is not a file", file=sys.stderr)
        return 2
    if image_path.stat().st_size > 5 * 1024 * 1024:
        print(
            f"error: {image_path} is {image_path.stat().st_size // 1024}KB — "
            "mattermost rejects profile images over 5MB",
            file=sys.stderr,
        )
        return 2
    if not agent_yml_path(agent_id).exists():
        print(f"error: agent {agent_id!r} not found", file=sys.stderr)
        return 2
    cfg = AgentConfig.load(agent_id)
    daemon = DaemonConfig.load()
    if not daemon.has_server_sync():
        print(
            "error: not logged in to a Puffo.ai server. run "
            "`puffoagent login --url ... --token ...` first so we "
            "have an admin token to update the bot's avatar with.",
            file=sys.stderr,
        )
        return 2
    bot_user_id = _resolve_bot_user_id(cfg.mattermost)
    if not bot_user_id:
        print("error: could not resolve bot user id — is the bot token valid?", file=sys.stderr)
        return 2
    ok, err = _upload_profile_image(
        cfg.mattermost.url, daemon.server.user_token,
        bot_user_id, image_path,
    )
    if not ok:
        print(f"error: image upload failed ({err})", file=sys.stderr)
        return 2
    print(f"agent {agent_id!r} avatar updated from {image_path.name}")
    return 0


def _resolve_bot_user_id(mm) -> str:
    """Call GET /users/me with the bot token to recover the bot's
    user id. Returns empty string on any failure.
    """
    import urllib.request
    import urllib.error
    import json as _json
    req = urllib.request.Request(
        mm.url.rstrip("/") + "/api/v4/users/me",
        headers={"Authorization": f"Bearer {mm.bot_token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
        return data.get("id", "")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError):
        return ""


def _patch_user_first_name(
    url: str, bot_token: str, bot_user_id: str, first_name: str,
) -> tuple[bool, str]:
    """PUT /api/v4/users/<id>/patch with a new first_name. This is
    a standard Mattermost v4 endpoint so no puffo-specific server
    changes are needed. The bot's own token is sufficient to patch
    its own profile.
    """
    import urllib.request
    import urllib.error
    import json as _json
    body = _json.dumps({"first_name": first_name}).encode("utf-8")
    req = urllib.request.Request(
        url.rstrip("/") + f"/api/v4/users/{bot_user_id}/patch",
        data=body,
        method="PUT",
        headers={
            "Authorization": f"Bearer {bot_token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
        return True, ""
    except urllib.error.HTTPError as exc:
        tail = exc.read().decode("utf-8", errors="replace")[:200]
        return False, f"{exc.code} {tail}"
    except (urllib.error.URLError, TimeoutError) as exc:
        return False, str(exc)


def _upload_profile_image(
    url: str, bot_token: str, bot_user_id: str, image_path: Path,
) -> tuple[bool, str]:
    """POST /api/v4/users/<bot_user_id>/image with the image as a
    multipart field named ``image``. Uses stdlib so this CLI path
    works without pulling aiohttp in to the main process.
    """
    import mimetypes
    import urllib.request
    import urllib.error
    import uuid

    boundary = f"----puffoagent{uuid.uuid4().hex}"
    mime = mimetypes.guess_type(image_path.name)[0] or "application/octet-stream"
    file_bytes = image_path.read_bytes()

    preamble = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="image"; filename="{image_path.name}"\r\n'
        f"Content-Type: {mime}\r\n\r\n"
    ).encode("utf-8")
    trailer = f"\r\n--{boundary}--\r\n".encode("utf-8")
    body = preamble + file_bytes + trailer

    req = urllib.request.Request(
        url.rstrip("/") + f"/api/v4/users/{bot_user_id}/image",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {bot_token}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
        return True, ""
    except urllib.error.HTTPError as exc:
        tail = exc.read().decode("utf-8", errors="replace")[:200]
        return False, f"{exc.code} {tail}"
    except (urllib.error.URLError, TimeoutError) as exc:
        return False, str(exc)


def cmd_agent_runtime(args: argparse.Namespace) -> int:
    """Update the runtime: block in an agent's agent.yml without needing
    a text editor. Every field is optional — only the ones you pass get
    changed. Invoke with no field flags to just print the current
    runtime block.
    """
    agent_id = args.id
    if not agent_yml_path(agent_id).exists():
        print(f"error: agent {agent_id!r} not found", file=sys.stderr)
        return 2
    cfg = AgentConfig.load(agent_id)

    touched = False
    if args.kind is not None:
        cfg.runtime.kind = args.kind
        touched = True
    if args.provider is not None:
        cfg.runtime.provider = args.provider
        touched = True
    if args.model is not None:
        cfg.runtime.model = args.model
        touched = True
    if args.api_key is not None:
        cfg.runtime.api_key = args.api_key
        touched = True
    if args.docker_image is not None:
        cfg.runtime.docker_image = args.docker_image
        touched = True
    if args.allowed_tools is not None:
        raw = args.allowed_tools.strip()
        cfg.runtime.allowed_tools = (
            [] if not raw else [t.strip() for t in raw.split(",") if t.strip()]
        )
        touched = True
    if args.permission_mode is not None:
        cfg.runtime.permission_mode = args.permission_mode
        touched = True
    if args.harness is not None:
        cfg.runtime.harness = args.harness
        touched = True

    if not touched:
        # No flags → just print. Matches `agent show`'s runtime lines.
        print(f"id:              {cfg.id}")
        print("runtime:")
        print(f"  kind:             {cfg.runtime.kind}")
        print(f"  harness:          {cfg.runtime.harness}  (cli-local / cli-docker)")
        print(f"  provider:         {cfg.runtime.provider or '(default)'}")
        print(f"  model:            {cfg.runtime.model or '(default)'}")
        print(f"  api_key:          {'(set)' if cfg.runtime.api_key else '(inherit)'}")
        print(f"  allowed_tools:    {cfg.runtime.allowed_tools or '[]'}")
        print(f"  docker_image:     {cfg.runtime.docker_image or '(bundled default)'}")
        print(f"  permission_mode:  {cfg.runtime.permission_mode}  (cli-local only)")
        return 0

    cfg.save()
    print(f"agent {agent_id!r} runtime updated:")
    print(f"  kind={cfg.runtime.kind} model={cfg.runtime.model or '(default)'}")
    if cfg.runtime.allowed_tools:
        print(f"  allowed_tools={cfg.runtime.allowed_tools}")
    if cfg.runtime.docker_image:
        print(f"  docker_image={cfg.runtime.docker_image}")
    if is_daemon_alive():
        print("daemon will restart the worker on the next reconcile tick.")
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
    sub.add_parser("version", help="Print installed puffoagent version").set_defaults(func=cmd_version)
    sub.add_parser(
        "check-update",
        help="Compare installed version against latest GitHub release",
    ).set_defaults(func=cmd_check_update)

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

    refresh_ping = agent_sub.add_parser(
        "refresh-ping",
        help=(
            "Diagnostic: run the OAuth refresh one-shot against a "
            "cli-local agent and print credentials before/after + "
            "full subprocess output."
        ),
    )
    refresh_ping.add_argument("id")
    refresh_ping.set_defaults(func=cmd_agent_refresh_ping)

    runtime = agent_sub.add_parser(
        "runtime",
        help="Show or edit the runtime: block in an agent's agent.yml",
    )
    runtime.add_argument("id")
    runtime.add_argument(
        "--kind",
        choices=["chat-only", "sdk", "cli-local", "cli-docker"],
        help="Runtime adapter kind",
    )
    runtime.add_argument("--provider", help="Chat-only: provider (anthropic|openai)")
    runtime.add_argument("--model", help="Model override (empty string clears)")
    runtime.add_argument("--api-key", help="Runtime API key (sdk/chat-only)")
    runtime.add_argument(
        "--allowed-tools",
        help="SDK: comma-separated tool allowlist patterns, e.g. Read,Edit,\"Bash(git *)\" — empty clears",
    )
    runtime.add_argument("--docker-image", help="cli-docker: override image tag")
    runtime.add_argument(
        "--permission-mode",
        choices=["default", "acceptEdits", "auto", "dontAsk", "bypassPermissions"],
        help=(
            "cli-local: Claude Code permission mode. 'default' routes all "
            "non-read tools through the MCP permission proxy (recommended). "
            "See https://code.claude.com/docs/en/permission-modes."
        ),
    )
    runtime.add_argument(
        "--harness",
        choices=["claude-code", "hermes"],
        help=(
            "cli-local / cli-docker: which agent engine runs inside the "
            "runtime. 'claude-code' (default) spawns the claude CLI with "
            "our stream-json session protocol. 'hermes' spawns `hermes chat` "
            "one-shot per turn against the Anthropic API using Claude "
            "Code's credential store. Hermes OAuth routes to Anthropic's "
            "extra_usage pool, NOT your Claude subscription — see "
            "NousResearch/hermes-agent#12905."
        ),
    )
    runtime.set_defaults(func=cmd_agent_runtime)

    archive = agent_sub.add_parser("archive", help="Stop and archive an agent to ~/.puffoagent/archived/")
    archive.add_argument("id")
    archive.set_defaults(func=cmd_agent_archive)

    edit = agent_sub.add_parser("edit", help="Open the agent's profile.md in $EDITOR")
    edit.add_argument("id")
    edit.set_defaults(func=cmd_agent_edit)

    rename = agent_sub.add_parser(
        "rename",
        help="Change the agent's display name (server-side + local)",
    )
    rename.add_argument("id")
    rename.add_argument(
        "display_name",
        help="New display name. UTF-8 / CJK / emoji are fine.",
    )
    rename.set_defaults(func=cmd_agent_rename)

    avatar = agent_sub.add_parser(
        "avatar",
        help="Set the agent bot's profile picture from an image file",
    )
    avatar.add_argument("id")
    avatar.add_argument(
        "image",
        help="Path to PNG/JPEG image (Mattermost recommends square, under 5MB)",
    )
    avatar.set_defaults(func=cmd_agent_avatar)

    export = agent_sub.add_parser("export", help="Export agent profile + memory + config as a zip")
    export.add_argument("id")
    export.add_argument("dest", help="Destination .zip file")
    export.set_defaults(func=cmd_agent_export)

    return parser


def main(argv: list[str] | None = None) -> int:
    # Force UTF-8 on stdout/stderr so non-ASCII display names, profile
    # excerpts, and Mattermost message bodies render correctly on
    # Windows consoles, which default to cp1252/cp936. Best-effort —
    # some Python distros (notably pyinstaller bundles) expose a
    # stdout without reconfigure(), so ignore any AttributeError.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
