"""``DockerCLIAdapter`` injects --memory and --memory-reservation
into the ``docker run`` argv when the corresponding fields are set.

Background: on a Docker Desktop VM with `vm.overcommit_memory=1`,
running multiple `puffo-*` containers without per-container caps
let one runaway claude exhaust the VM's swap, after which every
sibling container's small reads started returning kernel ENOMEM
(see v0.7.2 incident in CHANGELOG.md). Bounding each container
gives the kernel a clear blast radius — OOM kills land inside
the offender, not in its neighbours.

We test the argv-construction path only. The full ``_start_container``
is mocked at the boundary because it touches the filesystem and
spawns ``docker run`` for real.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from puffoagent.agent.adapters import docker_cli
from puffoagent.agent.adapters.docker_cli import DockerCLIAdapter


def _make_adapter(tmp_path, memory_limit="", memory_reservation=""):
    return DockerCLIAdapter(
        agent_id="t",
        model="",
        image="puffo/agent-runtime:test",
        workspace_dir=str(tmp_path / "ws"),
        claude_dir=str(tmp_path / "ws" / ".claude"),
        session_file=str(tmp_path / "session.json"),
        agent_home_dir=str(tmp_path / "home"),
        shared_fs_dir=str(tmp_path / "shared"),
        mcp_script_dir=str(tmp_path / "mcp"),
        memory_limit=memory_limit,
        memory_reservation=memory_reservation,
    )


def _capture_docker_run_argv(adapter) -> list[str]:
    """Drive ``_start_container`` with ``_run_cmd`` patched to a
    capture-and-noop stub. Returns the argv that would have been
    handed to ``docker run``.
    """
    captured: list[list[str]] = []

    async def _fake_run_cmd(cmd, check=True):
        captured.append(list(cmd))
        return 0, b"", b""

    # ``export_mcp_script`` does a real file write; stub it too so
    # the test doesn't care about MCP module presence on disk.
    with patch.object(docker_cli, "_run_cmd", new=_fake_run_cmd), \
         patch.object(docker_cli, "export_mcp_script", new=lambda d: None):
        asyncio.run(adapter._start_container())

    # Find the ``docker run`` invocation. Other ``docker`` calls
    # may have happened (image inspect, etc.) — pick the run.
    for cmd in captured:
        if len(cmd) >= 2 and cmd[0] == "docker" and cmd[1] == "run":
            return cmd
    raise AssertionError(
        f"no docker-run invocation captured; got: {captured!r}"
    )


def test_no_flags_when_both_unset(tmp_path):
    adapter = _make_adapter(tmp_path)
    argv = _capture_docker_run_argv(adapter)
    assert "--memory" not in argv
    assert "--memory-reservation" not in argv


def test_memory_flag_injected(tmp_path):
    adapter = _make_adapter(tmp_path, memory_limit="1.5g")
    argv = _capture_docker_run_argv(adapter)
    # --memory must appear with its value as the next token.
    idx = argv.index("--memory")
    assert argv[idx + 1] == "1.5g"
    assert "--memory-reservation" not in argv


def test_reservation_flag_injected(tmp_path):
    adapter = _make_adapter(tmp_path, memory_reservation="512m")
    argv = _capture_docker_run_argv(adapter)
    idx = argv.index("--memory-reservation")
    assert argv[idx + 1] == "512m"
    assert "--memory" not in argv


def test_both_flags_injected_in_expected_order(tmp_path):
    adapter = _make_adapter(
        tmp_path, memory_limit="1500m", memory_reservation="512m",
    )
    argv = _capture_docker_run_argv(adapter)
    assert argv.index("--memory") < argv.index("--memory-reservation")
    assert argv[argv.index("--memory") + 1] == "1500m"
    assert argv[argv.index("--memory-reservation") + 1] == "512m"


def test_flags_appear_before_image_token(tmp_path):
    """Docker rejects flags after the image positional. Both caps
    must be inserted *before* ``self.image`` in argv."""
    adapter = _make_adapter(
        tmp_path, memory_limit="1g", memory_reservation="256m",
    )
    argv = _capture_docker_run_argv(adapter)
    image_idx = argv.index("puffo/agent-runtime:test")
    assert argv.index("--memory") < image_idx
    assert argv.index("--memory-reservation") < image_idx
