import asyncio
from collections import defaultdict


class EventBus:
    """In-memory event bus — one asyncio.Queue per subscriber per session."""

    def __init__(self):
        self._subscribers: dict[str, list[asyncio.Queue]] = defaultdict(list)

    def subscribe(self, session_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers[session_id].append(queue)
        return queue

    def unsubscribe(self, session_id: str, queue: asyncio.Queue):
        subs = self._subscribers.get(session_id, [])
        if queue in subs:
            subs.remove(queue)

    async def publish(self, session_id: str, event: dict):
        for queue in self._subscribers.get(session_id, []):
            await queue.put(event)


event_bus = EventBus()
