"""Tests for Slack bot."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from app.event_bus import event_bus
from app.integrations.slack_bot import SlackBot


@pytest.mark.asyncio
async def test_handle_command_basic():
    bot = SlackBot()
    result = await bot.handle_command(
        "https://github.com/owner/repo Fix the login bug",
        channel_id="C123",
    )
    assert result["repo_url"] == "https://github.com/owner/repo"
    assert result["task_description"] == "Fix the login bug"
    assert "session_id" in result
    await bot.shutdown()


@pytest.mark.asyncio
async def test_handle_command_with_issue():
    bot = SlackBot()
    result = await bot.handle_command(
        "https://github.com/owner/repo #42 Fix the login bug",
        channel_id="C123",
    )
    assert result["repo_url"] == "https://github.com/owner/repo"
    assert result["issue_number"] == 42
    assert result["task_description"] == "Fix the login bug"
    assert "#42" in result["text"]
    await bot.shutdown()


@pytest.mark.asyncio
async def test_handle_command_empty():
    bot = SlackBot()
    result = await bot.handle_command("", channel_id="C123")
    assert "Usage" in result["text"]


@pytest.mark.asyncio
async def test_handle_command_missing_task():
    bot = SlackBot()
    result = await bot.handle_command("https://github.com/owner/repo", channel_id="C123")
    assert "Usage" in result["text"]


@pytest.mark.asyncio
async def test_format_status_change():
    bot = SlackBot()
    msg = bot._format_event({"event_type": "status_change", "payload": {"to": "running"}})
    assert msg == "Status: running"


@pytest.mark.asyncio
async def test_format_needs_human():
    bot = SlackBot()
    msg = bot._format_event({"event_type": "status_change", "payload": {"to": "needs_human"}})
    assert "needs help" in msg


@pytest.mark.asyncio
async def test_format_test_summary():
    bot = SlackBot()
    msg = bot._format_event({"event_type": "test_summary", "payload": {"passed": 10, "failed": 2}})
    assert "10 passed" in msg
    assert "2 failed" in msg


@pytest.mark.asyncio
async def test_format_pr_update():
    bot = SlackBot()
    msg = bot._format_event({"event_type": "pr_update", "payload": {"url": "https://github.com/pr/1"}})
    assert "https://github.com/pr/1" in msg


@pytest.mark.asyncio
async def test_event_listener_posts_to_slack():
    """Verify listener relays events to Slack client."""
    mock_client = AsyncMock()
    bot = SlackBot(slack_client=mock_client)

    session_id = "slack-test-1"

    # Start listener
    bot._start_listener(session_id, "C123", "ts123")

    # Give listener time to start
    await asyncio.sleep(0.05)

    # Publish events
    await event_bus.publish(session_id, {
        "event_type": "status_change",
        "payload": {"to": "running"},
    })
    await asyncio.sleep(0.05)

    await event_bus.publish(session_id, {
        "event_type": "test_summary",
        "payload": {"passed": 5, "failed": 0},
    })
    await asyncio.sleep(0.05)

    # Terminal event stops listener
    await event_bus.publish(session_id, {
        "event_type": "status_change",
        "payload": {"to": "completed"},
    })
    await asyncio.sleep(0.1)

    # Verify messages were posted
    assert mock_client.chat_postMessage.call_count >= 2
    calls = mock_client.chat_postMessage.call_args_list
    assert any("running" in str(c) for c in calls)

    await bot.shutdown()


@pytest.mark.asyncio
async def test_event_listener_ignores_unknown_events():
    """Unknown event types return None from format."""
    bot = SlackBot()
    msg = bot._format_event({"event_type": "unknown_event", "payload": {}})
    assert msg is None
    await bot.shutdown()
