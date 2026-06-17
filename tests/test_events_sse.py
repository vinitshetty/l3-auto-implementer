"""Tests for SSE streaming endpoint."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app
from app.models import HydraSession


@pytest.fixture
async def sse_engine():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def sse_client(sse_engine):
    from httpx import ASGITransport, AsyncClient

    factory = async_sessionmaker(sse_engine, class_=AsyncSession, expire_on_commit=False)

    async def override():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_sse_404_for_unknown_session(sse_client):
    resp = await sse_client.get("/api/sessions/unknown-id/events")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_sse_endpoint_exists(sse_engine):
    """Verify the SSE endpoint returns 200 for a valid session."""
    from httpx import ASGITransport, AsyncClient

    factory = async_sessionmaker(sse_engine, class_=AsyncSession, expire_on_commit=False)

    # Create a session first
    async with factory() as db:
        session = HydraSession(
            id="sse-test-1",
            repo_url="https://github.com/test/repo",
            task_description="test SSE",
            status="running",
        )
        db.add(session)
        await db.commit()

    async def override():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Use a short timeout — SSE streams forever, we just check it starts OK
        import asyncio
        try:
            async with asyncio.timeout(1.0):
                async with client.stream("GET", "/api/sessions/sse-test-1/events") as resp:
                    assert resp.status_code == 200
                    # Read at least one chunk (the SSE headers)
                    async for _ in resp.aiter_lines():
                        break
        except (asyncio.TimeoutError, TimeoutError):
            pass  # Expected — SSE streams indefinitely

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_event_bus_subscribe_unsubscribe():
    """Test event bus subscribe/unsubscribe logic directly."""
    from app.event_bus import EventBus

    bus = EventBus()
    q = bus.subscribe("test-session")
    assert len(bus._subscribers["test-session"]) == 1

    await bus.publish("test-session", {"event_type": "test", "payload": "hello"})
    event = q.get_nowait()
    assert event["event_type"] == "test"

    bus.unsubscribe("test-session", q)
    assert len(bus._subscribers["test-session"]) == 0
