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
    # Validate session and fetch past events, then release the DB session
    session = await db.get(HydraSession, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    result = await db.execute(
        select(SessionEvent)
        .where(SessionEvent.session_id == session_id)
        .order_by(SessionEvent.timestamp)
    )
    past_events = [
        {
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
        for evt in result.scalars().all()
    ]
    # Explicitly close the DB session before streaming — don't hold the pool connection
    await db.close()

    async def event_generator():
        # 1. Replay past events (already fetched)
        for evt in past_events:
            yield evt

        # 2. Stream live events from event bus (no DB needed)
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
