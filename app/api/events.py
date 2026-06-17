"""SSE streaming endpoint — replay past events from DB, then stream live."""

import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from app.database import get_db
from app.event_bus import event_bus
from app.models import HydraSession, SessionEvent

router = APIRouter()


@router.get("/sessions/{session_id}/events")
async def stream_events(session_id: str, db: AsyncSession = Depends(get_db)):
    session = await db.get(HydraSession, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    async def event_generator():
        # 1. Replay past events from DB
        result = await db.execute(
            select(SessionEvent)
            .where(SessionEvent.session_id == session_id)
            .order_by(SessionEvent.timestamp)
        )
        for evt in result.scalars().all():
            yield {
                "event": evt.event_type,
                "data": json.dumps({
                    "id": evt.id,
                    "event_type": evt.event_type,
                    "payload": evt.payload,
                    "span_id": evt.span_id,
                    "trace_id": evt.trace_id,
                    "timestamp": evt.timestamp.isoformat() if evt.timestamp else None,
                }),
            }

        # 2. Stream live events from event bus
        queue = event_bus.subscribe(session_id)
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30.0)
                    yield {
                        "event": event.get("event_type", "message"),
                        "data": json.dumps(event),
                    }
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": ""}
        finally:
            event_bus.unsubscribe(session_id, queue)

    return EventSourceResponse(event_generator())
