"""Tests for GitHub webhook handler."""

import hashlib
import hmac
import json

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.models import HydraSession
from app.api.webhooks import verify_signature


# --- Register webhook router in app ---

@pytest.fixture
async def webhook_client():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        echo=False,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    # Seed test data
    async with session_factory() as session:
        session.add(HydraSession(
            id="session-1",
            task_description="Fix bug",
            repo_url="https://github.com/test/repo",
            branch_name="hydra/abc12345",
            status="ci_monitoring",
            pr_url="https://github.com/test/repo/pull/1",
        ))
        await session.commit()

    async def override_get_db():
        async with session_factory() as session:
            yield session

    from app.main import app
    from app.api.webhooks import router as webhooks_router

    # Only include if not already included
    if webhooks_router not in [r for r in app.routes]:
        app.include_router(webhooks_router, prefix="/api")

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()
    await engine.dispose()


def _sign(payload: dict, secret: str = "") -> str:
    body = json.dumps(payload).encode()
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={sig}"


# --- Tests ---

def test_verify_signature():
    payload = b'{"test": true}'
    secret = "test-secret"
    sig = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    assert verify_signature(payload, sig, secret) is True
    assert verify_signature(payload, "sha256=invalid", secret) is False
    assert verify_signature(payload, "", secret) is False


async def test_issue_opened(webhook_client):
    payload = {
        "action": "opened",
        "issue": {"number": 42, "title": "Bug"},
        "repository": {"html_url": "https://github.com/test/repo"},
    }
    resp = await webhook_client.post(
        "/api/webhooks/github",
        json=payload,
        headers={"X-GitHub-Event": "issues"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "triage_queued"
    assert resp.json()["issue_number"] == 42


async def test_check_suite_completed(webhook_client):
    payload = {
        "action": "completed",
        "check_suite": {
            "head_branch": "hydra/abc12345",
            "status": "completed",
            "conclusion": "success",
        },
    }
    resp = await webhook_client.post(
        "/api/webhooks/github",
        json=payload,
        headers={"X-GitHub-Event": "check_suite"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ci_signal_queued"
    assert data["session_id"] == "session-1"


async def test_pr_review_changes_requested(webhook_client):
    payload = {
        "action": "submitted",
        "review": {
            "state": "changes_requested",
            "body": "Please fix the tests",
            "user": {"login": "reviewer"},
        },
        "pull_request": {
            "html_url": "https://github.com/test/repo/pull/1",
        },
    }
    resp = await webhook_client.post(
        "/api/webhooks/github",
        json=payload,
        headers={"X-GitHub-Event": "pull_request_review"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "review_signal_queued"
    assert data["action"] == "changes_requested"
    assert data["comments_count"] == 1


async def test_pr_review_approved(webhook_client):
    payload = {
        "action": "submitted",
        "review": {
            "state": "approved",
            "body": "LGTM",
            "user": {"login": "reviewer"},
        },
        "pull_request": {
            "html_url": "https://github.com/test/repo/pull/1",
        },
    }
    resp = await webhook_client.post(
        "/api/webhooks/github",
        json=payload,
        headers={"X-GitHub-Event": "pull_request_review"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "review_signal_queued"
    assert data["action"] == "approved"
    assert data["comments_count"] == 0


async def test_invalid_signature(webhook_client):
    from app.config import settings
    original = settings.github_webhook_secret
    settings.github_webhook_secret = "real-secret"

    try:
        payload = {"action": "opened", "issue": {"number": 1}}
        resp = await webhook_client.post(
            "/api/webhooks/github",
            json=payload,
            headers={
                "X-GitHub-Event": "issues",
                "X-Hub-Signature-256": "sha256=invalid",
            },
        )
        assert resp.status_code == 403
    finally:
        settings.github_webhook_secret = original


async def test_unknown_event(webhook_client):
    resp = await webhook_client.post(
        "/api/webhooks/github",
        json={"action": "created"},
        headers={"X-GitHub-Event": "star"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"
