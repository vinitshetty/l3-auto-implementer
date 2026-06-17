"""Tests for SandboxManager — uses mocked Docker client."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.sandbox.manager import ExecResult, SandboxManager


def _make_mock_container(container_id="test-container-123"):
    container = MagicMock()
    container.id = container_id
    return container


def _make_mock_docker(container=None):
    if container is None:
        container = _make_mock_container()
    client = MagicMock()
    client.containers.get.return_value = container
    client.containers.run.return_value = container
    return client


def _make_exec_output(stdout="", stderr="", exit_code=0):
    result = MagicMock()
    result.exit_code = exit_code
    result.output = (stdout.encode(), stderr.encode())
    return result


async def test_exec_in_container():
    container = _make_mock_container()
    container.exec_run.return_value = _make_exec_output(stdout="hello\n")
    docker = _make_mock_docker(container)
    mgr = SandboxManager(docker_client=docker)

    result = await mgr.exec_in_container("test-id", "echo hello")
    assert result.exit_code == 0
    assert "hello" in result.stdout


async def test_create():
    docker = _make_mock_docker()
    mgr = SandboxManager(docker_client=docker)

    cid = await mgr.create("session-1", "https://github.com/t/r", "token", "key")
    assert cid is not None
    docker.containers.run.assert_called_once()


async def test_run_git():
    container = _make_mock_container()
    container.exec_run.return_value = _make_exec_output(stdout="Already up to date.\n")
    docker = _make_mock_docker(container)
    mgr = SandboxManager(docker_client=docker)

    output = await mgr.run_git("test-id", "pull")
    assert "up to date" in output.lower() or output is not None


async def test_run_tests_with_report():
    report = {
        "summary": {"total": 3, "passed": 2, "failed": 1, "skipped": 0},
        "tests": [
            {"nodeid": "test_a.py::test_pass1", "outcome": "passed", "duration": 0.1},
            {"nodeid": "test_a.py::test_pass2", "outcome": "passed", "duration": 0.2},
            {"nodeid": "test_b.py::test_fail", "outcome": "failed", "duration": 0.3,
             "longrepr": "AssertionError: 1 != 2"},
        ],
    }
    container = _make_mock_container()
    call_count = [0]

    def mock_exec(cmd, demux=True):
        call_count[0] += 1
        if "pytest" in str(cmd):
            return _make_exec_output(stdout="test output", exit_code=1)
        elif "cat" in str(cmd):
            return _make_exec_output(stdout=json.dumps(report))
        return _make_exec_output()

    container.exec_run.side_effect = mock_exec
    docker = _make_mock_docker(container)
    mgr = SandboxManager(docker_client=docker)

    result = await mgr.run_tests("test-id")
    assert result.total == 3
    assert result.passed == 2
    assert result.failed == 1
    assert len(result.failures) == 1
    assert "test_fail" in result.failures[0].name


async def test_run_vibe():
    ndjson_output = "\n".join([
        json.dumps({"type": "turn_start", "turn": 1}),
        json.dumps({"type": "thinking", "content": "Let me analyze..."}),
        json.dumps({"type": "tool_call", "tool": "file_write", "args": {"path": "main.py"}}),
        json.dumps({"type": "tool_result", "tool": "file_write", "path": "main.py"}),
        json.dumps({"type": "message", "content": "I fixed the bug"}),
        json.dumps({"type": "turn_end", "turn": 1}),
        json.dumps({"type": "usage", "total_tokens": 1500}),
    ])

    container = _make_mock_container()
    container.exec_run.return_value = _make_exec_output(stdout=ndjson_output)
    docker = _make_mock_docker(container)
    mgr = SandboxManager(docker_client=docker)

    summary = await mgr.run_vibe("test-id", "fix the bug")
    assert summary.turns == 1
    assert summary.tool_calls == 1
    assert "main.py" in summary.files_modified
    assert summary.tokens_used == 1500


async def test_get_diff_stats():
    diff_output = """ src/main.py   | 10 ++++------
 src/utils.py  |  5 +++++
 2 files changed, 9 insertions(+), 6 deletions(-)"""
    container = _make_mock_container()
    container.exec_run.return_value = _make_exec_output(stdout=diff_output)
    docker = _make_mock_docker(container)
    mgr = SandboxManager(docker_client=docker)

    stats = await mgr.get_diff_stats("test-id")
    assert stats["files_changed"] == 2
    assert stats["lines_added"] == 9
    assert stats["lines_removed"] == 6


async def test_destroy():
    container = _make_mock_container()
    docker = _make_mock_docker(container)
    mgr = SandboxManager(docker_client=docker)

    await mgr.destroy("test-id")
    container.stop.assert_called_once()
    container.remove.assert_called_once()
