"""Slack bot — signal relay for Hydra sessions.

Handles /hydra slash command and posts thread updates from event bus.
"""

import asyncio
import re
import uuid
from typing import Any

from app.event_bus import event_bus


class SlackBot:
    """Slack bot that relays session events to Slack threads."""

    def __init__(self, slack_client: Any = None, db_factory=None):
        self.client = slack_client
        self.db_factory = db_factory
        self._tasks: dict[str, asyncio.Task] = {}

    async def handle_command(self, text: str, channel_id: str, thread_ts: str | None = None) -> dict:
        """Parse /hydra command and create a session.

        Format: /hydra <repo_url> [#issue_number] <task description>
        """
        text = text.strip()
        if not text:
            return {"text": "Usage: /hydra <repo_url> [#issue] <task description>"}

        parts = text.split(None, 1)
        if len(parts) < 2:
            return {"text": "Usage: /hydra <repo_url> [#issue] <task description>"}

        repo_url = parts[0]
        rest = parts[1]

        # Check for #issue_number
        issue_number = None
        issue_match = re.match(r"#(\d+)\s+(.*)", rest)
        if issue_match:
            issue_number = int(issue_match.group(1))
            task_description = issue_match.group(2)
        else:
            task_description = rest

        if not task_description.strip():
            return {"text": "Usage: /hydra <repo_url> [#issue] <task description>"}

        session_id = str(uuid.uuid4())

        # Create session in DB if factory is available
        if self.db_factory:
            from app.models import HydraSession

            async with self.db_factory() as db:
                session = HydraSession(
                    id=session_id,
                    repo_url=repo_url,
                    task_description=task_description,
                    issue_number=issue_number,
                    status="pending",
                )
                db.add(session)
                await db.commit()

        # Start listening for events
        self._start_listener(session_id, channel_id, thread_ts)

        result = {
            "text": f"Session created: {session_id}\nRepo: {repo_url}\nTask: {task_description}",
            "session_id": session_id,
            "repo_url": repo_url,
            "task_description": task_description,
        }
        if issue_number:
            result["text"] += f"\nIssue: #{issue_number}"
            result["issue_number"] = issue_number

        return result

    def _start_listener(self, session_id: str, channel_id: str, thread_ts: str | None):
        """Subscribe to event bus and post updates to Slack thread."""
        task = asyncio.create_task(
            self._listen_events(session_id, channel_id, thread_ts)
        )
        self._tasks[session_id] = task

    async def _listen_events(self, session_id: str, channel_id: str, thread_ts: str | None):
        """Listen for events and relay to Slack."""
        queue = event_bus.subscribe(session_id)
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=300.0)
                    message = self._format_event(event)
                    if message and self.client:
                        await self._post_message(channel_id, message, thread_ts)

                    # Stop listening on terminal states
                    event_type = event.get("event_type", "")
                    payload = event.get("payload", {})
                    if event_type == "status_change" and isinstance(payload, dict):
                        if payload.get("to") in ("completed", "failed", "cancelled"):
                            break
                except asyncio.TimeoutError:
                    continue
        finally:
            event_bus.unsubscribe(session_id, queue)

    def _format_event(self, event: dict) -> str | None:
        """Format event into Slack message text."""
        event_type = event.get("event_type", "")
        payload = event.get("payload", {})

        if event_type == "status_change" and isinstance(payload, dict):
            to_status = payload.get("to", "unknown")
            if to_status == "needs_human":
                return "Agent needs help — check the UI for details"
            return f"Status: {to_status}"

        if event_type == "test_summary" and isinstance(payload, dict):
            passed = payload.get("passed", 0)
            failed = payload.get("failed", 0)
            return f"Tests: {passed} passed / {failed} failed"

        if event_type == "pr_update" and isinstance(payload, dict):
            url = payload.get("url", "")
            return f"PR opened: {url}"

        return None

    async def _post_message(self, channel: str, text: str, thread_ts: str | None):
        """Post message to Slack channel/thread."""
        if hasattr(self.client, "chat_postMessage"):
            kwargs: dict[str, Any] = {"channel": channel, "text": text}
            if thread_ts:
                kwargs["thread_ts"] = thread_ts
            await self.client.chat_postMessage(**kwargs)

    async def shutdown(self):
        """Cancel all listener tasks."""
        for task in self._tasks.values():
            task.cancel()
        self._tasks.clear()
